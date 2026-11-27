#!/bin/bash
# M5 pipeline (M4 plus front gate, customer logs, usage records and the lease), run on the builder as root: from sources and the locked M3
# artifacts in the bucket to a signed GPU image plus every value the key
# policy needs. Nothing is pasted by hand.
# Inputs: /opt/smh/src (synced from the laptop), /mnt/m3/nvidia/{modules,rpms}
#         (signed kernel modules and verified packages), s3://.../m3/m4-artifacts.json
# Outputs: /mnt/m3/out/summary.json and the signed raw image in /mnt/m3/signed.
set -euo pipefail
# Settings (the deployer passes them; the defaults rebuild image 0.8.1 from the m9 objects):
#   PREFIX    bucket folder that holds the locked model objects
#   ART       name of the artifact list in that folder
#   VERSION   image version; written into the recipe and the usage records
REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}; PREFIX=${PREFIX:-m9}; ART=${ART:-artifacts.json}; VERSION=${VERSION:-0.8.1}
KEY_ALIAS=alias/smh-poc-model-key; LEASE_ALIAS=alias/smh-poc-lease-key
SRC=/opt/smh/src; WORK=${WORK:-/mnt/m5}; DESC=$WORK/desc; OUT=$WORK/out; NV=/mnt/m3/nvidia
export PYTHONPATH=$SRC
mkdir -p "$OUT"

echo "== 1. runtime list from the locked artifacts =="
aws s3 cp --only-show-errors --region $REGION "s3://$BUCKET/$PREFIX/$ART" "$WORK/m4-artifacts.json"
KEY_ARN=$(aws kms describe-key --region $REGION --key-id $KEY_ALIAS --query KeyMetadata.Arn --output text)
LEASE_ARN=$(aws kms describe-key --region $REGION --key-id $LEASE_ALIAS --query KeyMetadata.Arn --output text)
python3 - "$WORK" "$BUCKET" "$KEY_ARN" <<'PY'
import sys, json
from sealbox import manifest, loader
work, bucket, key_arn = sys.argv[1:4]
m = {"bucket": bucket, "key_arn": key_arn, "images": json.load(open(work + "/m4-artifacts.json"))["images"]}
manifest.validate(m)
json.dump(m, open(work + "/manifest.json", "w"), indent=1)
json.dump({"manifest_digest": manifest.digest(m), "pcr15_open": loader.expected_open_pcr(m),
           "pcr15_locked": loader.expected_locked_pcr(m)}, open(work + "/runtime.json", "w"))
print(json.dumps(m["images"]))
PY

echo "== 2. check the NVIDIA inputs =="
# "our key" is whichever db key this build machine holds: the test one, or the builder's own.
SIGNER="$(openssl x509 -in /opt/smh/sb/db.crt -noout -subject -nameopt multiline | sed -n 's/^ *commonName *= *//p')"
K=$(ls "$NV"/modules/nvidia.ko >/dev/null && modinfo -F vermagic "$NV/modules/nvidia.ko" | cut -d' ' -f1)
for m in "$NV"/modules/*.ko; do
  [ "$(modinfo -F signer "$m")" = "$SIGNER" ] || { echo "module not signed with our key: $m"; exit 1; }
done
rpm -K "$NV"/rpms/*.rpm | grep -v "digests signatures OK" && { echo "package signature problem"; exit 1; }
echo "modules for kernel $K, signed with our key; $(ls "$NV"/rpms/*.rpm | wc -l) packages verified"

echo "== 3. assemble the image description and build =="
rm -rf "$DESC" "$WORK/image"; mkdir -p "$WORK/image"
cp -r "$SRC/image" "$DESC"
rm -rf "$DESC/attic-m1" "$DESC/container" "$DESC/pipeline" "$DESC/probe" "$DESC/m3" "$DESC/m4" "$DESC/m5" "$DESC/secureboot"
mkdir -p "$DESC/root/usr/local/lib/smh/sealbox" "$DESC/root/usr/local/lib/smh/nvidia-modules" "$DESC/root/etc/smh" "$DESC/root/var/tmp/smh-rpms/keys"
cp "$SRC"/sealbox/{__init__,manifest,envelope,loader,proxy,jobform,jobrunner,lease,meter,logship,gatekeys}.py "$DESC/root/usr/local/lib/smh/sealbox/"
cp "$SRC/image/check-image.sh" "$DESC/root/usr/local/lib/smh/"
cp "$WORK/manifest.json" "$DESC/root/etc/smh/manifest.json"
python3 - "$SRC/image/m5/loader.json" "$WORK/m4-artifacts.json" "$DESC/root/etc/smh/loader.json" "$LEASE_ARN" "$VERSION" "$PREFIX" <<'PY'
import sys, json
cfg = json.load(open(sys.argv[1])); art = json.load(open(sys.argv[2]))
cfg["jobs"]["params_per_rank"] = art["params_per_rank"]   # fixes how large an adapter may be
cfg["lease"]["key_arn"] = sys.argv[4]
cfg["usage"]["image"] = sys.argv[5]          # the version and the folder come from the pipeline's settings,
cfg["prefix"] = sys.argv[6]                   # so one recipe serves every release
json.dump(cfg, open(sys.argv[3], "w"), indent=1)
print("adapter size limits from the model:", art["params_per_rank"])
PY
cp "$NV"/modules/*.ko "$DESC/root/usr/local/lib/smh/nvidia-modules/"
cp "$NV"/rpms/*.rpm "$DESC/root/var/tmp/smh-rpms/"
cp /etc/pki/rpm-gpg/RPM-GPG-KEY-NVIDIA-D42D0685 /etc/pki/rpm-gpg/RPM-GPG-KEY-amazon-linux-2023-nvidia "$DESC/root/var/tmp/smh-rpms/keys/"
chmod +x "$DESC/edit_boot_install.sh" "$DESC"/root/usr/local/sbin/*
sed -i "s|<version>[0-9.]*</version>|<version>$VERSION</version>|" "$DESC/appliance.kiwi"
kiwi-ng --loglevel 20 --logfile "$WORK/kiwi.log" system build --description "$DESC" --target-dir "$WORK/image" >"$WORK/kiwi.out" 2>&1 \
  || { echo "BUILD FAILED"; grep -E "CHECK-IMAGE|KiwiError|Error:|error:|No match|nothing provides" "$WORK/kiwi.log" | tail -20 | cut -c1-260; exit 1; }
grep -h "CHECK-IMAGE" "$WORK/kiwi.log" | sed 's/.*CHECK-IMAGE/CHECK-IMAGE/' | sort -u
RAW=$(ls "$WORK"/image/*.raw)
IMGKERNEL=$(ls "$WORK/image/build/image-root/lib/modules" | head -1)
[ "$IMGKERNEL" = "$K" ] || { echo "KERNEL MISMATCH: image has $IMGKERNEL, modules are for $K"; exit 1; }
echo "image kernel $IMGKERNEL matches the signed modules"
# Every kernel module the driver depends on must be in the image, or the
# driver will not load on the GPU machine.
ROOT="$WORK/image/build/image-root"; missing=0
for m in nvidia nvidia-uvm; do
  for dep in $(modinfo -F depends "$NV/modules/$m.ko" | tr ',' ' '); do
    case "$dep" in nvidia*) continue;; esac
    if modinfo -b "$ROOT" -k "$K" "$dep" >/dev/null 2>&1 || grep -q "/${dep//_/[-_]}\.ko" "$ROOT/lib/modules/$K/modules.builtin"; then :; else echo "MISSING kernel module needed by $m: $dep"; missing=1; fi
  done
done
[ "$missing" = 0 ] || exit 1
echo "all kernel modules the driver depends on are present"
# Every program the boot scripts, loader and job runner call must be in the image.
for b in aws docker zstd bash openssl base64 curl ss cryptsetup findmnt lsblk mkfs.xfs modprobe python3 \
         tpm2_pcrextend tpm2_pcrread nitro-tpm-attest nginx journalctl systemctl iptables systemd-run getent nvidia-smi nvidia-modprobe nvidia-container-runtime; do
  found=$(ls "$ROOT"/usr/bin/$b "$ROOT"/usr/sbin/$b "$ROOT"/usr/local/bin/$b 2>/dev/null | head -1 || true)
  [ -n "$found" ] || { echo "MISSING program in the image: $b"; missing=1; }
done
[ "$missing" = 0 ] || exit 1
echo "all programs the loader and job runner call are present"

echo "== 4. sign for Secure Boot and compute the PCR values =="
rm -rf "$WORK/signed"   # an older signed image left here would be picked up by the release scripts
bash "$SRC/image/secureboot/make-signed.sh" "$RAW" "$WORK/signed" | tail -9

echo "== 5. summary =="
python3 - "$WORK" "$OUT" "$VERSION" <<'PY'
import sys, json
work, out, version = sys.argv[1:4]
pcr = json.load(open(work + "/signed/pcr_measurements.json"))["Measurements"]
rt = json.load(open(work + "/runtime.json"))
s = {"version": version, "PCR4": pcr["PCR4"], "PCR7": pcr["PCR7"], "PCR12": pcr["PCR12"],
     "PCR15_open": rt["pcr15_open"], "PCR15_locked": rt["pcr15_locked"], "manifest_digest": rt["manifest_digest"],
     "manifest": json.load(open(work + "/manifest.json")), "artifacts": json.load(open(work + "/m4-artifacts.json"))}
json.dump(s, open(out + "/summary.json", "w"), indent=1)
print(json.dumps({k: v for k, v in s.items() if k not in ("manifest", "artifacts")}, indent=1))
PY
cp "$WORK/signed/uefi-data.blob" "$WORK"/image/*.packages "$OUT/"
# NO_UPLOAD=1 with another WORK: a second build for comparison only (assurance test S10).
[ -n "${NO_UPLOAD:-}" ] || aws s3 sync --only-show-errors --region $REGION "$OUT/" "s3://$BUCKET/build/out-$VERSION/"
echo "PIPELINE OK"

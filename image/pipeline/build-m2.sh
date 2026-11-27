#!/bin/bash
# M2 pipeline, run on the builder as root. One command from sources to a signed
# image plus every value the key policy needs. Nothing is pasted by hand.
#   build-m2.sh [stage]     stage = all (default) | image   (image: reuse the archive)
# Inputs (synced from the laptop): /opt/smh/src/image, /opt/smh/src/sealbox
# Outputs: /mnt/m2/out/summary.json and the signed raw image.
set -euo pipefail
STAGE=${1:-all}
REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}; PREFIX=m2
KEY_ALIAS=alias/smh-poc-model-key
SRC=/opt/smh/src; WORK=/mnt/m2; DESC=$WORK/desc; OUT=$WORK/out
NAME=model-server
export PYTHONPATH=$SRC
mkdir -p "$WORK" "$OUT"

if [ "$STAGE" = all ]; then
  echo "== 1. build the test container =="
  dnf -y -q install docker python3-cryptography >/dev/null
  systemctl start docker
  docker build -q -t smh/$NAME:latest "$SRC/image/container"
  docker save smh/$NAME:latest -o "$WORK/$NAME.tar"

  echo "== 2. encrypt it with a fresh data key (plain key stays in memory) =="
  aws kms generate-data-key --region $REGION --key-id $KEY_ALIAS --key-spec AES_256 --output json | python3 -c '
import sys, json, base64
from sealbox import envelope
d = json.load(sys.stdin); work, name = sys.argv[1], sys.argv[2]
open(work + "/wrapped.key", "wb").write(base64.b64decode(d["CiphertextBlob"]))
with open(f"{work}/{name}.tar", "rb") as src, open(f"{work}/{name}.sbox", "wb") as dst:
    print("chunks:", envelope.encrypt_stream(src, dst, base64.b64decode(d["Plaintext"])))
' "$WORK" $NAME
  rm -f "$WORK/$NAME.tar"
  aws s3 cp --only-show-errors --region $REGION "$WORK/wrapped.key" "s3://$BUCKET/$PREFIX/wrapped.key"
  aws s3 cp --only-show-errors --region $REGION "$WORK/$NAME.sbox" "s3://$BUCKET/$PREFIX/$NAME.sbox"
fi

echo "== 3. write the runtime list =="
KEY_ARN=$(aws kms describe-key --region $REGION --key-id $KEY_ALIAS --query KeyMetadata.Arn --output text)
python3 - "$WORK" "$BUCKET" "$KEY_ARN" $NAME <<'PY'
import sys, json, hashlib
from sealbox import manifest, loader
work, bucket, key_arn, name = sys.argv[1:5]
m = {"bucket": bucket, "key_arn": key_arn,
     "images": [{"name": name, "digest": loader._sha256(f"{work}/{name}.sbox")}]}
manifest.validate(m)
json.dump(m, open(work + "/manifest.json", "w"), indent=1)
json.dump({"manifest_digest": manifest.digest(m), "pcr15_open": loader.expected_open_pcr(m)},
          open(work + "/runtime.json", "w"))
print(json.dumps(m, indent=1))
PY

echo "== 4. assemble the image description and build =="
rm -rf "$DESC" "$WORK/image"; mkdir -p "$WORK/image"
cp -r "$SRC/image" "$DESC"
rm -rf "$DESC/attic-m1" "$DESC/container" "$DESC/pipeline"
mkdir -p "$DESC/root/usr/local/lib/smh/sealbox" "$DESC/root/etc/smh"
cp "$SRC"/sealbox/{__init__,manifest,envelope,loader}.py "$DESC/root/usr/local/lib/smh/sealbox/"
cp "$SRC/image/check-image.sh" "$DESC/root/usr/local/lib/smh/"
cp "$WORK/manifest.json" "$DESC/root/etc/smh/manifest.json"
chmod +x "$DESC/edit_boot_install.sh" "$DESC"/root/usr/local/sbin/*
[ -n "${EXTRA_PACKAGE:-}" ] && sed -i "/<ignore name=\"$EXTRA_PACKAGE\"\/>/d" "$DESC/appliance.kiwi"
[ -n "${EXTRA_PACKAGE:-}" ] && sed -i "s|<package name=\"openssl\"/>|<package name=\"openssl\"/><package name=\"$EXTRA_PACKAGE\"/>|" "$DESC/appliance.kiwi"
kiwi-ng --loglevel 20 --logfile "$WORK/kiwi.log" system build --description "$DESC" --target-dir "$WORK/image" >"$WORK/kiwi.out" 2>&1 \
  || { echo "BUILD FAILED"; grep -E "CHECK-IMAGE|KiwiError|Error:" "$WORK/kiwi.log" | tail -15; exit 1; }
grep -h "CHECK-IMAGE" "$WORK/kiwi.log" | sed 's/.*CHECK-IMAGE/CHECK-IMAGE/' | sort -u
RAW=$(ls "$WORK"/image/*.raw)
[ -n "${SKIP_SIGN:-}" ] && { cat "$WORK/image/pcr_measurements.json"; exit 0; }

echo "== 5. sign for Secure Boot and compute the PCR values =="
bash "$SRC/image/secureboot/make-signed.sh" "$RAW" "$WORK/signed" | tail -12

echo "== 6. summary =="
python3 - "$WORK" "$OUT" <<'PY'
import sys, json
work, out = sys.argv[1:3]
pcr = json.load(open(work + "/signed/pcr_measurements.json"))["Measurements"]
rt = json.load(open(work + "/runtime.json"))
s = {"version": "0.2.0", "PCR4": pcr["PCR4"], "PCR7": pcr["PCR7"], "PCR12": pcr["PCR12"],
     "PCR15_open": rt["pcr15_open"], "manifest_digest": rt["manifest_digest"],
     "manifest": json.load(open(work + "/manifest.json"))}
json.dump(s, open(out + "/summary.json", "w"), indent=1)
print(json.dumps({k: v for k, v in s.items() if k != "manifest"}, indent=1))
PY
cp "$WORK/signed/uefi-data.blob" "$WORK"/image/*.packages "$OUT/"
aws s3 sync --only-show-errors --region $REGION "$OUT/" "s3://$BUCKET/build/out-0.2.0/"
echo "PIPELINE OK"

#!/bin/bash
# Run on the build machine as root (the release agent starts it as the fixed step "setup"): turn a fresh Amazon
# Linux 2023 machine into one that can build releases. Safe to run again: it installs what is missing, NEVER
# replaces a key or the lease secret, and rebuilds the NVIDIA modules only when the image's kernel has moved on.
#
# Your own Secure Boot keys: before the first run, put PK.key PK.crt KEK.key KEK.crt db.key db.crt into
# /opt/smh/sb (root only). Whatever is missing there is made as a TEST key.
# shortcut: private keys are plain files on this disk, fine for a POC; a real pipeline signs through KMS or an HSM.
set -euo pipefail
REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}
SB=/opt/smh/sb; NV=/mnt/m3/nvidia; REPORT=/var/lib/smh-setup.json; P="$(cd "$(dirname "$0")" && pwd)"
RPMS="libnvidia-container-tools libnvidia-container1 nvidia-container-toolkit nvidia-container-toolkit-base nvidia-driver-common
nvidia-driver-cuda nvidia-driver-cuda-libs nvidia-kmod-common nvidia-modprobe nvidia-persistenced"

echo "== 1. build tools, from the Amazon Linux repository only =="
dnf -y -q install kiwi-cli kiwi-systemdeps aws-nitro-tpm-tools erofs-utils veritysetup tpm2-tools pesign qemu-img openssl \
  efitools python3-uefivars docker python3-cryptography zstd nss-tools dnf-plugins-core
systemctl enable --now docker >/dev/null 2>&1
for b in kiwi-ng pesign nitro-tpm-pcr-compute uefivars.py cert-to-efi-sig-list certutil veritysetup mkfs.erofs docker zstd; do
  command -v "$b" >/dev/null || { echo "MISSING program: $b"; exit 1; }
done
echo "tools present"

echo "== 2. Secure Boot keys =="
umask 077; mkdir -p "$SB"; cd "$SB"; KEYS=own
[ -f GUID.txt ] || uuidgen --random > GUID.txt
for k in PK KEK db; do
  if [ ! -f $k.key ]; then
    [ ! -f $k.crt ] || { echo "found $k.crt without $k.key: put both in $SB, or neither"; exit 1; }
    openssl req -newkey rsa:4096 -nodes -keyout $k.key -new -x509 -sha256 -days 3650 -subj "/CN=smh-poc test $k/" -out $k.crt 2>/dev/null; KEYS=test
  fi
  [ -f $k.esl ] || cert-to-efi-sig-list -g "$(< GUID.txt)" $k.crt $k.esl
done
[ -f db.p12 ] || openssl pkcs12 -export -out db.p12 -inkey db.key -in db.crt -name smhdb -passout pass:
[ -d nss ] || { mkdir -p nss && certutil -N -d nss --empty-password && pk12util -d nss -i db.p12 -W "" >/dev/null; }
SIGNER="$(openssl x509 -in db.crt -noout -subject -nameopt multiline | sed -n 's/^ *commonName *= *//p')"
case "$SIGNER" in "smh-poc test db") KEYS=test;; esac
echo "keys present ($KEYS); modules will be signed as \"$SIGNER\""
umask 022; cd /

echo "== 3. NVIDIA kernel modules for the kernel the image will get, signed with the db key =="
dnf -y -q install nvidia-release
# The image recipe takes the newest Amazon Linux kernel; the modules must be built for exactly that one.
KV="$(dnf -q repoquery --releasever=latest --latest-limit 1 --qf '%{version}-%{release}' kernel)"; K="$KV.x86_64"
[ -n "$KV" ] || { echo "could not find the kernel version in the repository"; exit 1; }
have() { modinfo -F "$1" "$NV/modules/nvidia.ko" 2>/dev/null || true; }
if [ "$(have vermagic | cut -d' ' -f1)" = "$K" ] && [ "$(have signer)" = "$SIGNER" ]; then
  echo "modules for $K are already here"
else
  dnf -y -q install --releasever=latest "kernel-devel-$KV"
  dnf -y -q install kmod-nvidia-open-dkms || true     # its own build is for the RUNNING kernel and may fail; ours is below
  mkdir -p "/lib/modules/$K"; ln -sfn "/usr/src/kernels/$K" "/lib/modules/$K/build"    # the driver's makefile looks here
  V="$(dkms status | grep -o 'nvidia/[0-9.]*' | head -1 | cut -d/ -f2)"
  [ -n "$V" ] || { echo "the NVIDIA driver source is not installed"; exit 1; }
  dkms build "nvidia/$V" -k "$K" >/var/log/smh-nvidia-build.log 2>&1 || { echo "NVIDIA BUILD FAILED"; tail -5 /var/log/smh-nvidia-build.log; exit 1; }
  rm -rf /mnt/m3/nvidia/modules.new /mnt/m3/nvidia/rpms.new; mkdir -p "$NV/modules.new" "$NV/rpms.new"
  for m in /var/lib/dkms/nvidia/"$V"/"$K"/x86_64/module/*.ko*; do
    o="$NV/modules.new/$(basename "$m" | sed 's/\.ko.*/.ko/')"
    case "$m" in *.zst) zstd -dqf "$m" -o "$o";; *.xz) xz -dc "$m" > "$o";; *) cp "$m" "$o";; esac
    # DKMS signs with a throwaway key the sealed machine does not trust: take that signature off, put ours on.
    python3 - "$o" <<'PY'
import sys
MAGIC = b"~Module signature appended~\n"
d = open(sys.argv[1], "rb").read()
while d.endswith(MAGIC):
    info = d[-len(MAGIC) - 12:-len(MAGIC)]        # struct module_signature: ..., signer_len, key_id_len, pad, sig_len (big endian)
    d = d[:-(len(MAGIC) + 12 + info[3] + info[4] + int.from_bytes(info[8:12], "big"))]
open(sys.argv[1], "wb").write(d)
PY
    "/usr/src/kernels/$K/scripts/sign-file" sha512 "$SB/db.key" "$SB/db.crt" "$o"
    [ "$(modinfo -F signer "$o")" = "$SIGNER" ] || { echo "signing failed for $o"; exit 1; }
  done
  # The userspace packages must be the same driver version as the modules, so they are fetched together.
  # shellcheck disable=SC2086
  (cd "$NV/rpms.new" && dnf -q download $RPMS)
  rpm -K "$NV"/rpms.new/*.rpm | grep -v "digests signatures OK" && { echo "package signature problem"; exit 1; }
  # Swap in; the previous set is kept once, as .old.
  rm -rf /mnt/m3/nvidia/modules.old /mnt/m3/nvidia/rpms.old
  for d in modules rpms; do [ ! -d "$NV/$d" ] || mv "$NV/$d" "$NV/$d.old"; mv "$NV/$d.new" "$NV/$d"; done
  echo "built driver $V for $K: $(ls "$NV"/modules/*.ko | wc -l) modules signed, $(ls "$NV"/rpms/*.rpm | wc -l) packages verified"
fi
DRIVER="$(modinfo -F version "$NV/modules/nvidia.ko")"

echo "== 4. the lease secret (seals the usage records) =="
if [ -s /opt/smh/lease/seal.key ]; then LEASE=present
elif aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key m5/lease.token >/dev/null 2>&1; then
  LEASE=elsewhere; echo "a lease token exists in the bucket, but its secret is not on this machine: it is NOT replaced"
elif aws kms describe-key --region "$REGION" --key-id alias/smh-poc-lease-key >/dev/null 2>&1; then
  bash "$P/prepare-lease.sh"; LEASE=present
else LEASE=no-lease-key; echo "the lease key does not exist yet: create it, then run this again"
fi
echo "lease secret: $LEASE"

READY=false; [ "$LEASE" = present ] && READY=true
printf '{"ready": %s, "keys": "%s", "kernel": "%s", "driver": "%s", "packages": %s, "lease_secret": "%s", "disk_free_gib": %s}\n' \
  "$READY" "$KEYS" "$K" "$DRIVER" "$(ls "$NV"/rpms/*.rpm | wc -l)" "$LEASE" "$(df --output=avail -BG / | tail -1 | tr -dc 0-9)" | tee "$REPORT"
echo "SETUP DONE"

#!/bin/bash
# Run on the builder as root. Makes TEST Secure Boot keys, signs the boot file
# inside a copy of the raw image, computes the PCR values, and writes the UEFI
# key store ("blob") that register-image takes as --uefi-data.
# shortcut: private keys are plain files on the builder, fine for a POC;
# a real pipeline keeps the signing key in KMS or an HSM.
set -euo pipefail
SRC=${1:-/mnt/image/smh-sealed.x86_64-0.1.0.raw}
SB=/opt/smh/sb; OUT=${2:-/mnt/image-signed}; ESP_OFFSET=$((2048*512)); RAW=$(basename "$SRC")
dnf -y -q install efitools python3-uefivars >/dev/null
umask 077; mkdir -p "$SB" "$OUT"; cd "$SB"

if [ ! -f db.key ]; then
  uuidgen --random > GUID.txt
  for k in PK KEK db; do
    openssl req -newkey rsa:4096 -nodes -keyout $k.key -new -x509 -sha256 -days 3650 -subj "/CN=smh-poc test $k/" -out $k.crt 2>/dev/null
    cert-to-efi-sig-list -g "$(< GUID.txt)" $k.crt $k.esl
  done
  openssl pkcs12 -export -out db.p12 -inkey db.key -in db.crt -name smhdb -passout pass:
  mkdir -p nss && certutil -N -d nss --empty-password && pk12util -d nss -i db.p12 -W "" >/dev/null
fi

cp --sparse=always "$SRC" "$OUT/$RAW"
M=$(mktemp -d); mount -o loop,offset=$ESP_OFFSET "$OUT/$RAW" "$M"
trap 'umount "$M" 2>/dev/null || true' EXIT
BOOT=$(ls "$M"/EFI/BOOT/BOOT*.EFI)
pesign -n nss -c smhdb -s -i "$BOOT" -o "$OUT/uki.signed.efi" --force
cp "$OUT/uki.signed.efi" "$BOOT"; sync; umount "$M"; trap - EXIT
echo "signature:"; pesign -S -i "$OUT/uki.signed.efi" | grep -E "common name|No signatures" | head -3

nitro-tpm-pcr-compute --image "$OUT/uki.signed.efi" --PK PK.esl --KEK KEK.esl --db db.esl | tee "$OUT/pcr_measurements.json"
uefivars.py -i none -o aws -O "$OUT/uefi-data.blob" -P PK.esl -K KEK.esl --db db.esl
echo "blob bytes: $(wc -c < "$OUT/uefi-data.blob")"
chmod 644 "$OUT/pcr_measurements.json" "$OUT/uefi-data.blob"

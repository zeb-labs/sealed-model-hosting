#!/bin/bash
# Run on the builder as root, after build-m5.sh: make the attack material for
# the assurance suite's break tier. TEST ONLY; none of it is ever released.
#   /mnt/m6/b2.raw   the signed image plus two files added to the boot partition (gap A6)
#   /mnt/m6/b3.raw   the signed image with one changed byte in block 0 of the system partition
#   s3://.../m6/model.tensors.tampered.sbox   the encrypted weights (from folder PREFIX, default m9) with one changed byte
set -euo pipefail
REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}
RAW=$(ls /mnt/m5/signed/*.raw); mkdir -p /mnt/m6; cd /mnt/m6
part() { lsblk -ln -o PATH,FSTYPE "$1" | awk -v t="$2" '$2==t{print $1}' | head -1; }

cp --sparse=always "$RAW" b2.raw
L=$(losetup -fP --show b2.raw); sleep 1
ESP=$(part "$L" vfat); [ -n "$ESP" ] || { echo "no boot partition found"; losetup -d "$L"; exit 1; }
mkdir -p esp; mount "$ESP" esp
mkdir -p esp/EFI/BOOT/BOOTX64.EFI.extra.d esp/loader/credentials
echo "smh break test" > esp/EFI/BOOT/BOOTX64.EFI.extra.d/smh.cred
echo "smh break test" > esp/loader/credentials/smh.cred
umount esp; losetup -d "$L"
echo "b2.raw: two files added to the boot partition"

cp --sparse=always "$RAW" b3.raw
L=$(losetup -fP --show b3.raw); sleep 1
ROOT=$(part "$L" erofs); [ -n "$ROOT" ] || { echo "no system partition found"; losetup -d "$L"; exit 1; }
before=$(dd if="$ROOT" bs=1 skip=2048 count=1 status=none | od -An -tu1 | tr -d ' ')
printf "\\$(printf '%03o' $(( (before + 1) % 256 )))" | dd of="$ROOT" bs=1 seek=2048 conv=notrunc status=none
losetup -d "$L"
echo "b3.raw: byte 2048 of the system partition changed from $before"

aws s3 cp --only-show-errors --region $REGION "s3://$BUCKET/${PREFIX:-m9}/model.tensors.sbox" w.sbox
size=$(stat -c %s w.sbox); at=$((size / 2))
before=$(dd if=w.sbox bs=1 skip=$at count=1 status=none | od -An -tu1 | tr -d ' ')
printf "\\$(printf '%03o' $(( (before + 1) % 256 )))" | dd of=w.sbox bs=1 seek=$at conv=notrunc status=none
aws s3 cp --only-show-errors --region $REGION w.sbox "s3://$BUCKET/m6/model.tensors.tampered.sbox"; rm w.sbox
echo "tampered weights uploaded: byte $at of $size changed"
ls -la /mnt/m6/*.raw

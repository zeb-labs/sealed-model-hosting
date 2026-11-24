#!/bin/bash
# KIWI calls this after installing the boot files: $1 = raw disk image, $2 = root device.
# 1. Make the UKI the file the firmware boots directly (no boot menu in between),
#    so PCR4 measures exactly this one file.
# 2. Compute the reference PCR values from it.
set -e -o pipefail
build_dir=$(dirname "$1")
root_mount=$(findmnt -n -o TARGET "$2")
efi="$root_mount/boot/efi"
mountpoint -q "$efi" || { echo "ERROR: EFI partition not mounted at $efi"; exit 1; }
[ -f "$efi/EFI/Linux/kiwi.efi" ] || { echo "ERROR: UKI not found"; ls -R "$efi/EFI"; exit 1; }

boot_name=$(find "$efi/EFI/BOOT/" -name "BOOT*.EFI" -printf "%f\n" | head -n1)
[ -n "$boot_name" ] || { echo "ERROR: no BOOT*.EFI in $efi/EFI/BOOT"; exit 1; }
cp "$efi/EFI/Linux/kiwi.efi" "$efi/EFI/BOOT/$boot_name"
rm -rf "$efi/EFI/Linux" "$efi/EFI/systemd"

cp "$efi/EFI/BOOT/$boot_name" "$build_dir/uki.efi"   # kept for inspection and signing tests
"$root_mount/usr/bin/nitro-tpm-pcr-compute" --image "$efi/EFI/BOOT/$boot_name" | tee "$build_dir/pcr_measurements.json"
echo "ESP contents:"; find "$efi" -type f

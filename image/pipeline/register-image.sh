#!/bin/bash
# Run on the builder as root: turn the signed raw image into an AMI.
# The builder-side twin of infra/m1/publish-image.sh, for the deployer:
# blank disk -> attach to this machine -> write image -> snapshot -> register.
#   VERSION=0.7.0 bash register-image.sh        prints "AMI ami-... SNAPSHOT snap-..."
set -euo pipefail
: "${VERSION:?version}"; WORK=${WORK:-/mnt/m5}; REGION=us-east-1
A="aws --region $REGION --no-cli-pager"
RAW=$(ls "$WORK"/signed/*.raw); [ "$(echo "$RAW" | wc -l)" = 1 ] || { echo "expected exactly one signed image"; exit 1; }
TOK=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
md() { curl -s -H "X-aws-ec2-metadata-token: $TOK" "http://169.254.169.254/latest/meta-data/$1"; }
IID=$(md instance-id); AZ=$(md placement/availability-zone)
T="{Key=business-unit,Value=ai},{Key=env,Value=innovation},{Key=category,Value=rnd},{Key=owner,Value=owner@example.com},{Key=app,Value=Secure-model-hosting},{Key=expire-date,Value=recurring}"
GB=$(( ($(stat -c %s "$RAW") + (1 << 30) - 1) >> 30 ))

VOL=$($A ec2 create-volume --availability-zone "$AZ" --size "$GB" --volume-type gp3 \
  --tag-specifications "ResourceType=volume,Tags=[{Key=Name,Value=smh-poc-image-staging-$VERSION},$T]" --query VolumeId --output text)
trap '$A ec2 detach-volume --volume-id "$VOL" >/dev/null 2>&1 || true; $A ec2 wait volume-available --volume-ids "$VOL" 2>/dev/null || true; $A ec2 delete-volume --volume-id "$VOL" || echo "WARNING: delete $VOL by hand"' EXIT
$A ec2 wait volume-available --volume-ids "$VOL"
$A ec2 attach-volume --volume-id "$VOL" --instance-id "$IID" --device /dev/sdf >/dev/null
$A ec2 wait volume-in-use --volume-ids "$VOL"
serial=${VOL/-/}; dev=""
for _ in $(seq 1 30); do dev=$(lsblk -dn -o NAME,SERIAL | awk -v s="$serial" '$2==s{print $1}'); [ -n "$dev" ] && break; sleep 2; done
[ -n "$dev" ] || { echo "disk not found"; exit 1; }
[ "$(lsblk -n "/dev/$dev" | wc -l)" = 1 ] || { echo "disk is not blank"; exit 1; }
dd if="$RAW" of="/dev/$dev" bs=4M conv=fsync status=none; sync
$A ec2 detach-volume --volume-id "$VOL" >/dev/null; $A ec2 wait volume-available --volume-ids "$VOL"
SNAP=$($A ec2 create-snapshot --volume-id "$VOL" --description "smh-poc sealed image $VERSION" \
  --tag-specifications "ResourceType=snapshot,Tags=[{Key=Name,Value=smh-poc-sealed-$VERSION},$T]" --query SnapshotId --output text)
for _ in 1 2 3 4; do $A ec2 wait snapshot-completed --snapshot-ids "$SNAP" && break; done
AMI=$($A ec2 register-image --name "smh-poc-sealed-$VERSION-$(date -u +%Y%m%d%H%M%S)" \
  --root-device-name /dev/xvda --architecture x86_64 --virtualization-type hvm \
  --boot-mode uefi --tpm-support v2.0 --ena-support --uefi-data "$(cat "$WORK/signed/uefi-data.blob")" \
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={SnapshotId=$SNAP,VolumeType=gp3,DeleteOnTermination=true}" \
  --tag-specifications "ResourceType=image,Tags=[{Key=Name,Value=smh-poc-sealed-$VERSION},$T]" --query ImageId --output text)
echo "AMI $AMI SNAPSHOT $SNAP"

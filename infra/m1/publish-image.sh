#!/usr/bin/env bash
# Turn the raw image on the builder into an AMI, without the EBS direct APIs:
# blank disk -> attach to builder -> write image -> snapshot -> register AMI.
#   bash infra/m1/publish-image.sh <label>      e.g. m1-0.1.0
# Prints the AMI id. The temporary disk is deleted at the end.
set -euo pipefail
PROFILE="poc-ino"; ACCOUNT="111122223333"; REGION="us-east-1"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="${1:?label, e.g. m1-0.1.0}"
RAW="${RAW:-/mnt/image/smh-sealed.x86_64-0.1.0.raw}"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }

T="{Key=business-unit,Value=ai},{Key=env,Value=innovation},{Key=category,Value=rnd},{Key=owner,Value=owner@example.com},{Key=app,Value=Secure-model-hosting},{Key=expire-date,Value=recurring}"
out() { $A cloudformation describe-stacks --stack-name smh-poc-m1-builder --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
IID="$(out InstanceId)"; AZ="$(out Az)"

VOL="$($A ec2 create-volume --availability-zone "$AZ" --size "${SIZE_GB:-6}" --volume-type gp3 \
  --tag-specifications "ResourceType=volume,Tags=[{Key=Name,Value=smh-poc-image-staging-$LABEL},$T]" --query VolumeId --output text)"
echo "volume $VOL"
trap 'echo "cleanup: deleting $VOL"; $A ec2 detach-volume --volume-id "$VOL" >/dev/null 2>&1 || true; $A ec2 wait volume-available --volume-ids "$VOL" 2>/dev/null || true; $A ec2 delete-volume --volume-id "$VOL" || echo "WARNING: delete $VOL by hand"' EXIT
$A ec2 wait volume-available --volume-ids "$VOL"
$A ec2 attach-volume --volume-id "$VOL" --instance-id "$IID" --device /dev/sdf --query State --output text
$A ec2 wait volume-in-use --volume-ids "$VOL"

# On the builder: find the disk by its serial (the volume id), refuse anything else.
TIMEOUT=1200 bash "$DIR/ssm.sh" "
set -e
serial=${VOL/-/}
for i in \$(seq 1 30); do dev=\$(lsblk -dn -o NAME,SERIAL | awk -v s=\$serial '\$2==s{print \$1}'); [ -n \"\$dev\" ] && break; sleep 2; done
[ -n \"\$dev\" ] || { echo 'disk not found'; exit 1; }
[ \"\$(lsblk -n /dev/\$dev | wc -l)\" = 1 ] || { echo 'disk is not blank'; exit 1; }
dd if=$RAW of=/dev/\$dev bs=4M conv=fsync status=none
sync; echo \"wrote $RAW to /dev/\$dev\"; sha256sum $RAW | cut -c1-64
"
$A ec2 detach-volume --volume-id "$VOL" --query State --output text
$A ec2 wait volume-available --volume-ids "$VOL"

SNAP="$($A ec2 create-snapshot --volume-id "$VOL" --description "smh-poc sealed image $LABEL" \
  --tag-specifications "ResourceType=snapshot,Tags=[{Key=Name,Value=smh-poc-sealed-$LABEL},$T]" --query SnapshotId --output text)"
echo "snapshot $SNAP"
# large images can take longer than one waiter round (10 minutes)
for _ in 1 2 3 4; do $A ec2 wait snapshot-completed --snapshot-ids "$SNAP" && break; done

AMI="$($A ec2 register-image --name "smh-poc-sealed-$LABEL-$(date -u +%Y%m%d%H%M%S)" \
  --root-device-name /dev/xvda --architecture x86_64 --virtualization-type hvm \
  --boot-mode uefi --tpm-support v2.0 --ena-support ${UEFI_DATA:+--uefi-data "$UEFI_DATA"} \
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={SnapshotId=$SNAP,VolumeType=gp3,DeleteOnTermination=true}" \
  --tag-specifications "ResourceType=image,Tags=[{Key=Name,Value=smh-poc-sealed-$LABEL},$T]" --query ImageId --output text)"
echo "AMI $AMI SNAPSHOT $SNAP"

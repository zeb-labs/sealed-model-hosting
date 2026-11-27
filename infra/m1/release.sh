#!/usr/bin/env bash
# After a pipeline run on the builder: write the key policy from the pipeline's
# values and register the signed image as an AMI.
#   bash infra/m1/release.sh <version> <builder work dir> <label> <evidence dir>
#   e.g. bash infra/m1/release.sh 0.4.0 /mnt/m4 m4-0.4.0 evidence/m4/build-0.4.0
# Writes <evidence dir>/ami-id.txt ("ami snap"). Each step logs to its own file.
set -euo pipefail
VERSION="$1"; WORK="$2"; LABEL="$3"; EV="$4"
A="aws --profile ${SMH_PROFILE:-poc-ino} --region ${SMH_REGION:-us-east-1} --no-cli-pager"
DIR="$(cd "$(dirname "$0")" && pwd)"; mkdir -p "$EV"
[ "$($A sts get-caller-identity --query Account --output text)" = "${SMH_ACCOUNT:-111122223333}" ] || { echo "STOP: wrong account"; exit 1; }
for f in summary.json uefi-data.blob; do $A s3 cp --only-show-errors "s3://smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}/build/out-$VERSION/$f" "$EV/$f"; done
eval "$(python3 -c "
import json,sys; s=json.load(open(sys.argv[1]))
print(f\"export PCR4={s['PCR4']} PCR7={s['PCR7']} PCR12={s['PCR12']} PCR15={s['PCR15_open']}\")" "$EV/summary.json")"
bash "$DIR/deploy.sh" key > "$EV/key-deploy.log" 2>&1
# The lease key pins the same image in the LOCKED state (M5 on).
LOCKED="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('PCR15_locked',''))" "$EV/summary.json")"
[ -z "$LOCKED" ] || PCR15="$LOCKED" bash "$DIR/deploy.sh" lease > "$EV/lease-deploy.log" 2>&1
echo "key policy written (PCR4 ${PCR4:0:12}...)"
RAW="$(bash "$DIR/ssm.sh" "ls $WORK/signed/*.raw" | grep '\.raw' | head -1)"
UEFI_DATA="$(cat "$EV/uefi-data.blob")" SIZE_GB="${SIZE_GB:-10}" RAW="$RAW" bash "$DIR/publish-image.sh" "$LABEL" > "$EV/publish.log" 2>&1 \
  || { echo "REGISTRATION FAILED"; tail -5 "$EV/publish.log"; exit 1; }
AMI=$(grep -o 'AMI ami-[0-9a-f]*' "$EV/publish.log" | cut -d' ' -f2); SNAP=$(grep -o 'snap-[0-9a-f]*' "$EV/publish.log" | head -1)
echo "$AMI $SNAP" > "$EV/ami-id.txt"; echo "registered $AMI ($SNAP)"

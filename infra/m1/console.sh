#!/usr/bin/env bash
# Wait for the sealed test machine to finish booting, save its console, print the result lines.
#   bash infra/m1/console.sh <evidence-file-name>
set -euo pipefail
A="aws --profile ${SMH_PROFILE:-poc-ino} --region ${SMH_REGION:-us-east-1} --no-cli-pager"
[ "$($A sts get-caller-identity --query Account --output text)" = "${SMH_ACCOUNT:-111122223333}" ] || { echo "STOP: wrong account"; exit 1; }
IID="$($A cloudformation describe-stacks --stack-name smh-poc-m1-test --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)"
OUT="$(cd "$(dirname "$0")/../.." && pwd)/evidence/${1:?evidence file, e.g. m2/t1-good.txt}"; mkdir -p "$(dirname "$OUT")"
for _ in $(seq 1 "${TRIES:-32}"); do
  $A ec2 get-console-output --instance-id "$IID" --latest --query Output --output text > "$OUT" 2>/dev/null || true
  grep -q "SMH DONE\|Kernel panic - not syncing\|No bootable device" "$OUT" && break
  sleep 15
done
echo "instance $IID -> $OUT ($(grep -c '' "$OUT") lines, login prompts: $(grep -c 'login:' "$OUT" || true))"
grep -E "SMH |Kernel panic - not syncing|No bootable device|CHECK-IMAGE" "$OUT" | sed -E 's/^\[ *[0-9.]+\] [a-z-]+\[[0-9]+\]: //' | grep -vE "PCR\[[a-z]+\] +sha384:" | cut -c1-230 | head -45 || true

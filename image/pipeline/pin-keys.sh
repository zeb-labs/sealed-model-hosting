#!/bin/bash
# Design step 5, run on the builder: write both key policies for $VERSION.
# The values come ONLY from the pipeline's own output file. Refuses unless a
# human's approval record names exactly that file's fingerprint (the Lambda
# checks this too; this is the second lock).
set -euo pipefail
: "${VERSION:?version}"; REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}
A="aws --region $REGION --no-cli-pager"; T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
$A s3 cp --only-show-errors "s3://$BUCKET/build/out-$VERSION/summary.json" "$T/summary.json"
$A s3 cp --only-show-errors "s3://$BUCKET/approvals/$VERSION.json" "$T/approval.json" || { echo "NOT APPROVED"; exit 3; }
eval "$(python3 - "$T" "$VERSION" <<'PY'
import hashlib, json, sys
t, version = sys.argv[1], sys.argv[2]
raw = open(t + "/summary.json", "rb").read(); s = json.loads(raw); a = json.load(open(t + "/approval.json"))
if a.get("version") != version or a.get("summary_sha256") != hashlib.sha256(raw).hexdigest() or s.get("version") != version:
    print("echo 'APPROVAL DOES NOT MATCH THIS BUILD'; exit 3"); sys.exit()
for k in ("PCR4", "PCR7", "PCR12", "PCR15_open", "PCR15_locked"):
    assert len(s[k]) == 96 and int(s[k], 16) >= 0
print(f"PCR4={s['PCR4']} PCR7={s['PCR7']} PCR12={s['PCR12']} OPEN={s['PCR15_open']} LOCKED={s['PCR15_locked']}")
PY
)"
pin() {   # pin <stack> <runtime value>: same template, same other settings, new measured values
  local out
  if out=$($A cloudformation update-stack --stack-name "$1" --use-previous-template --capabilities CAPABILITY_NAMED_IAM \
      --parameters ParameterKey=AdminUserArn,UsePreviousValue=true ParameterKey=Alias,UsePreviousValue=true ParameterKey=CustomerRoleArn,UsePreviousValue=true \
        ParameterKey=Pcr4,ParameterValue="$PCR4" ParameterKey=Pcr7,ParameterValue="$PCR7" \
        ParameterKey=Pcr12,ParameterValue="$PCR12" ParameterKey=Pcr15,ParameterValue="$2" 2>&1); then
    $A cloudformation wait stack-update-complete --stack-name "$1"
  else
    echo "$out" | grep -q "No updates are to be performed" || { echo "$out"; return 1; }
  fi
  echo "pinned $1"
}
pin smh-poc-m1-key "$OPEN"
pin smh-poc-m1-lease "$LOCKED"
echo "PIN OK"

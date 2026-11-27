#!/usr/bin/env bash
# Run a shell script on the builder through Session Manager and print its output.
#   bash infra/m1/ssm.sh 'uname -a'            inline commands
#   bash infra/m1/ssm.sh - < script.sh         script from stdin
#   TIMEOUT=3600 bash infra/m1/ssm.sh '...'    longer run (seconds, default 600)
#   STACK=smh-poc-m1-gpu-helper bash infra/m1/ssm.sh '...'   run on another machine of ours
set -euo pipefail
PROFILE="${SMH_PROFILE:-poc-ino}"; ACCOUNT="${SMH_ACCOUNT:-111122223333}"; REGION="${SMH_REGION:-us-east-1}"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }

IID="$($A cloudformation describe-stacks --stack-name "${STACK:-smh-poc-m1-builder}" --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)"
SCRIPT="$1"; [ "$SCRIPT" = "-" ] && SCRIPT="$(cat)"
T="${TIMEOUT:-600}"
PARAMS="$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]],"executionTimeout":[sys.argv[2]]}))' "$SCRIPT" "$T")"

CID="$($A ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript --timeout-seconds 60 \
  --parameters "$PARAMS" --query Command.CommandId --output text)"
while :; do
  ST="$($A ssm get-command-invocation --command-id "$CID" --instance-id "$IID" --query Status --output text 2>/dev/null || echo Pending)"
  case "$ST" in Pending|InProgress|Delayed) sleep 5;; *) break;; esac
done
$A ssm get-command-invocation --command-id "$CID" --instance-id "$IID" --query '[StandardOutputContent,StandardErrorContent]' --output text
echo "== status: $ST =="
[ "$ST" = "Success" ]

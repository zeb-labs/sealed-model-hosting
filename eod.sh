#!/usr/bin/env bash
# End-of-day check for the Secure Model Hosting POC.
#   bash eod.sh          list everything we created (read-only)
#   bash eod.sh --stop   also stop our running EC2 instances
# Deleting is never automatic: do it per resource, after the owner says yes.
set -euo pipefail

PROFILE="${SMH_PROFILE:-poc-ino}"; ACCOUNT="${SMH_ACCOUNT:-111122223333}"; REGION="${SMH_REGION:-us-east-1}"
TAG_KEY="app"; TAG_VALUE="Secure-model-hosting"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"

got="$($A sts get-caller-identity --query Account --output text)"
[ "$got" = "$ACCOUNT" ] || { echo "STOP: account is $got, not $ACCOUNT"; exit 1; }

echo "== Resources tagged $TAG_KEY=$TAG_VALUE in $REGION =="
$A resourcegroupstaggingapi get-resources --tag-filters "Key=$TAG_KEY,Values=$TAG_VALUE" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n' | sed '/^$/d'

# IAM and budgets are global and not fully covered by the tagging API above.
echo "== IAM roles and instance profiles under /zl/ named smh-poc-* =="
$A iam list-roles --path-prefix /zl/ --query "Roles[?starts_with(RoleName,'smh-poc')].Arn" --output text | tr '\t' '\n' | sed '/^$/d'
$A iam list-instance-profiles --path-prefix /zl/ --query "InstanceProfiles[?starts_with(InstanceProfileName,'smh-poc')].Arn" --output text | tr '\t' '\n' | sed '/^$/d'
echo "== Budgets named smh-poc-* =="
$A budgets describe-budgets --account-id "$ACCOUNT" --query "Budgets[?starts_with(BudgetName,'smh-poc')].[BudgetName,BudgetLimit.Amount,CalculatedSpend.ActualSpend.Amount]" --output text 2>/dev/null || true

echo "== Estimated POC spend (cost guard) =="
$A ssm get-parameter --name /smh-poc/cost-guard/state --query Parameter.Value --output text 2>/dev/null || echo "cost guard not deployed"

echo "== Our running EC2 instances =="
running="$($A ec2 describe-instances \
  --filters "Name=tag:$TAG_KEY,Values=$TAG_VALUE" Name=instance-state-name,Values=pending,running \
  --query 'Reservations[].Instances[].InstanceId' --output text)"
echo "${running:-none}"

if [ "${1:-}" = "--stop" ] && [ -n "$running" ]; then
  echo "Stopping: $running"
  $A ec2 stop-instances --instance-ids $running --query 'StoppingInstances[].[InstanceId,CurrentState.Name]' --output text
fi

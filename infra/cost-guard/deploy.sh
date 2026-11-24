#!/usr/bin/env bash
# Deploy, update or remove the smh-poc cost guard.
#   bash infra/cost-guard/deploy.sh            create or update
#   bash infra/cost-guard/deploy.sh --delete   remove everything it made
# Change the limit: LIMIT=700 STOP_AT=650 bash infra/cost-guard/deploy.sh
# Move the nightly stop (UTC cron): NIGHTLY='cron(30 15 * * ? *)' bash infra/cost-guard/deploy.sh
#   21:00 IST = cron(30 15 * * ? *).  09:00 IST = cron(30 3 * * ? *).
set -euo pipefail
PROFILE="poc-ino"; ACCOUNT="111122223333"; REGION="us-east-1"; STACK="smh-poc-cost-guard"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"
DIR="$(cd "$(dirname "$0")" && pwd)"

got="$($A sts get-caller-identity --query Account --output text)"
[ "$got" = "$ACCOUNT" ] || { echo "STOP: account is $got, not $ACCOUNT"; exit 1; }

if [ "${1:-}" = "--delete" ]; then
  $A cloudformation delete-stack --stack-name "$STACK"
  $A cloudformation wait stack-delete-complete --stack-name "$STACK"
  echo "deleted $STACK"; exit 0
fi

# The six tags the SCP requires. Stack tags are copied onto every resource.
$A cloudformation deploy --stack-name "$STACK" --template-file "$DIR/template.yaml" \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --parameter-overrides AlertEmail="owner@example.com" LimitUsd="${LIMIT:-500}" StopAtUsd="${STOP_AT:-450}" ${NIGHTLY:+NightlyStopCronUtc="$NIGHTLY"} \
  --tags business-unit=ai env=innovation category=rnd owner=owner@example.com app=Secure-model-hosting expire-date=recurring
$A cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].[StackStatus,Outputs]' --output json

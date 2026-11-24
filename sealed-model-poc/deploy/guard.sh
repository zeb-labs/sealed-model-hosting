#!/usr/bin/env bash
# Account safety check for the proof of concept.
#
# Every deploy script calls this first. It stops unless the AWS account you
# are logged in to is exactly the one you named. It never uses the default
# profile: you must name the profile yourself.
#
# Usage (in your own terminal):
#   export POC_ACCOUNT_ID=123456789012     # the account you chose for the POC
#   export POC_REGION=us-east-1            # the region you chose
#   export AWS_PROFILE=my-poc-profile      # the login to use, named explicitly
#   bash deploy/guard.sh                   # prints OK, or STOP with the reason
#
# Deploy scripts start with:   "$(dirname "$0")/guard.sh" || exit 1
set -euo pipefail

: "${POC_ACCOUNT_ID:?Set POC_ACCOUNT_ID to the 12-digit account chosen for this POC}"
: "${POC_REGION:?Set POC_REGION to the region chosen for this POC}"
: "${AWS_PROFILE:?Set AWS_PROFILE explicitly. The default profile is never used}"

if ! [[ "$POC_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "STOP: POC_ACCOUNT_ID must be exactly 12 digits." >&2
  exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
  echo "STOP: the AWS command-line tool is not installed." >&2
  exit 1
fi

actual_account="$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" --region "$POC_REGION" \
  --query Account --output text)"

if [[ "$actual_account" != "$POC_ACCOUNT_ID" ]]; then
  echo "STOP: profile '$AWS_PROFILE' is logged in to account $actual_account," >&2
  echo "      but this POC is set to account $POC_ACCOUNT_ID. Nothing was changed." >&2
  exit 1
fi

echo "OK: using account $POC_ACCOUNT_ID in $POC_REGION via profile '$AWS_PROFILE'."

#!/usr/bin/env bash
# Put one encrypted release into the CUSTOMER's bucket, so the sealed machine
# can load it from there (launch it with SOURCE=s3://<customer bucket>/model/<folder>).
# Everything copied is ciphertext; the keys stay in the builder's KMS.
#   bash infra/m1/publish-to-customer.sh m9
set -euo pipefail
F="${1:?release folder, for example m9}"; PROFILE="poc-ino"; ACCOUNT="111122223333"
A="aws --profile $PROFILE --region us-east-1 --no-cli-pager"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }
SRC="s3://smh-poc-artifacts-$ACCOUNT"; DST="s3://smh-poc-customer-$ACCOUNT/model/$F"
CKEY="$($A kms describe-key --key-id alias/smh-poc-customer-key --query KeyMetadata.Arn --output text)"
$A s3 ls "$SRC/$F/lease.token" >/dev/null 2>&1 || $A s3 cp --only-show-errors "$SRC/m5/lease.token" "$SRC/$F/lease.token"
$A s3 sync --only-show-errors --sse aws:kms --sse-kms-key-id "$CKEY" "$SRC/$F/" "$DST/"
$A s3 ls "$DST/" | awk '{print $3, $4}'
echo "published. Launch with: SOURCE=$DST"

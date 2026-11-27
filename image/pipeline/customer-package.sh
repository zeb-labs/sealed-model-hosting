#!/bin/bash
# Design step 6, the builder's half: put together what a customer needs to
# install the sealed machine in THEIR account. Nothing is created here.
#   customer-package.sh <gpu type>
set -euo pipefail
: "${VERSION:?version}" "${PREFIX:?folder}"; GPU="$1"; REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}; SRC=/opt/smh/src
A="aws --region $REGION --no-cli-pager"; T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
$A s3 cp --only-show-errors "s3://$BUCKET/build/out-$VERSION/ami-id.txt" "$T/ami-id.txt"
python3 - "$T" "$VERSION" "$PREFIX" "$GPU" "$BUCKET" \
  "$($A kms describe-key --key-id alias/smh-poc-model-key --query KeyMetadata.Arn --output text)" \
  "$($A kms describe-key --key-id alias/smh-poc-lease-key --query KeyMetadata.Arn --output text)" <<'PY'
import json, sys
t, version, prefix, gpu, bucket, model_key, lease_key = sys.argv[1:8]
json.dump({"version": version, "image": open(t + "/ami-id.txt").read().split()[0], "instance_type": gpu,
           "builder_bucket": bucket, "folder": prefix, "model_key": model_key, "lease_key": lease_key,
           "templates": ["customer.yaml", "control.yaml", "test.yaml"],
           "launch": "private subnet, no key pair, IMDSv2 with hop limit 1, the sealed instance role"},
          open(t + "/install.json", "w"), indent=1)
PY
for f in customer.yaml control.yaml test.yaml; do $A s3 cp --only-show-errors "$SRC/infra/m1/$f" "s3://$BUCKET/build/out-$VERSION/customer-install/$f"; done
$A s3 cp --only-show-errors "$T/install.json" "s3://$BUCKET/build/out-$VERSION/customer-install/install.json"
# The lease token travels with the release, so a copy in the customer's bucket is complete.
$A s3 cp --only-show-errors "s3://$BUCKET/m5/lease.token" "s3://$BUCKET/$PREFIX/lease.token"
echo "PACKAGE OK"

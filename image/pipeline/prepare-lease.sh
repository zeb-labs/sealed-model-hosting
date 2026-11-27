#!/bin/bash
# Run on the builder as root, once per lease key: make the lease token.
# One KMS call returns a fresh 32-byte secret in two forms:
#  - wrapped by the lease key  -> s3://.../m5/lease.token (the sealed machine
#    can unwrap it only in the LOCKED state of our exact image)
#  - plain                     -> /opt/smh/lease/seal.key on this builder,
#    root only. It is the seal key for usage records (sealbox/meter.py).
# The plain secret is never printed and never leaves this disk.
set -euo pipefail
REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}
install -d -m 0700 /opt/smh/lease; umask 077
aws kms generate-data-key --region $REGION --key-id alias/smh-poc-lease-key --key-spec AES_256 --output json | python3 -c '
import sys, json, base64
d = json.load(sys.stdin)
open("/opt/smh/lease/lease.token", "wb").write(base64.b64decode(d["CiphertextBlob"]))
open("/opt/smh/lease/seal.key", "wb").write(base64.b64decode(d["Plaintext"]))
'
aws s3 cp --only-show-errors --region $REGION /opt/smh/lease/lease.token "s3://$BUCKET/m5/lease.token"
echo "lease token uploaded ($(wc -c </opt/smh/lease/lease.token) bytes); seal key kept on the builder ($(wc -c </opt/smh/lease/seal.key) bytes)"

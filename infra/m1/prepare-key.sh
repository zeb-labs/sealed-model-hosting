#!/usr/bin/env bash
# Make the M1 test material and upload it:
#   m1/wrapped.key  a data key wrapped by the model key (only KMS can unwrap it)
#   m1/canary.enc   a test sentence encrypted with that data key
# The plain data key exists only in this process's memory and is never written.
set -euo pipefail
PROFILE="poc-ino"; ACCOUNT="111122223333"; REGION="us-east-1"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }
BUCKET="smh-poc-artifacts-$ACCOUNT"; TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
SENTENCE="${1:-The sealed machine unwrapped the key.}"

# One call returns both forms. Python keeps the plain key in memory, encrypts
# the sentence with openssl (key passed as hex through a pipe), writes only ciphertext.
$A kms generate-data-key --key-id alias/smh-poc-model-key --key-spec AES_256 --output json | python3 -c '
import sys, json, base64, subprocess, os
d = json.load(sys.stdin); out, sentence = sys.argv[1], sys.argv[2]
open(out + "/wrapped.key", "wb").write(base64.b64decode(d["CiphertextBlob"]))
r, w = os.pipe(); os.write(w, base64.b64decode(d["Plaintext"]).hex().encode()); os.close(w)
subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-out", out + "/canary.enc", "-pass", f"fd:{r}"],
               input=sentence.encode(), pass_fds=[r], check=True)
' "$TMP" "$SENTENCE"
$A s3 cp --only-show-errors "$TMP/wrapped.key" "s3://$BUCKET/m1/wrapped.key"
$A s3 cp --only-show-errors "$TMP/canary.enc" "s3://$BUCKET/m1/canary.enc"
echo "uploaded m1/wrapped.key ($(wc -c <"$TMP/wrapped.key" | tr -d ' ') bytes) and m1/canary.enc"

#!/bin/bash
# Run on the builder as root: the builder's view of the customer's account.
# Reads the usage records (allowed) and checks seals and numbering. Then shows
# that the builder's role cannot read anything else on the customer's side.
set -uo pipefail
REGION="${SMH_REGION:-us-east-1}"; CUSTOMER=smh-poc-customer-${SMH_ACCOUNT:-111122223333}; D=/mnt/m5/usage
rm -rf "$D"; mkdir -p "$D"
aws s3 sync --only-show-errors --region $REGION "s3://$CUSTOMER/usage/" "$D/"
od -An -v -tx1 /opt/smh/lease/seal.key | tr -d ' \n' | PYTHONPATH=/opt/smh/src python3 -m sealbox.meter check "$D"
echo "check exit code: $?"
for other in jobs/ data/ adapters/ results/; do
  if aws s3 ls --region $REGION "s3://$CUSTOMER/$other" >/dev/null 2>&1; then echo "PROBLEM: builder can list $other"; else echo "builder cannot list $other (expected)"; fi
done

# With "tamper": show that a changed and a deleted record are caught (on copies; the bucket is not touched).
if [ "${1:-}" = tamper ]; then
  key() { od -An -v -tx1 /opt/smh/lease/seal.key | tr -d ' \n'; }
  C=/mnt/m5/usage-changed; rm -rf "$C"; cp -r "$D" "$C"
  g=$(find "$C" -name '*.json' | sort | sed -n 2p)
  echo "== change one number =="
  sed -i 's/"tokens_out": [0-9]*/"tokens_out": 987654/' "$g"; key | PYTHONPATH=/opt/smh/src python3 -m sealbox.meter check "$C"
  echo "== delete one record =="
  rm "$g"; key | PYTHONPATH=/opt/smh/src python3 -m sealbox.meter check "$C"
  echo "== customer view, no key =="
  echo -n "" | PYTHONPATH=/opt/smh/src python3 -m sealbox.meter check "$D"
  rm -rf "$C"
fi

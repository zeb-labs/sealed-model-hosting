# Sealed model POC

Proof of concept for secure model hosting: a model builder's weights run inside
a customer's AWS account without the customer being able to read them.

Background and design: "Secure Model Hosting: Problem Statement, Gaps and Our
Contribution" (the project document).

## Status

| Part | State |
| --- | --- |
| Lock and unlock weight files in tamper-checked chunks | Built and tested (offline) |
| Fine-tuning job form gatekeeper | Built and tested (offline) |
| Runtime list and its fingerprint | Built and tested (offline) |
| AWS account safety check | Built. Syntax and refusal paths checked. Not yet run against AWS |
| Key release from KMS to a proven machine | Not built yet. Needs the chosen AWS account |
| Sealed machine image | Not built yet. Needs the chosen AWS account |
| Serving with vLLM, and the sealed trainer | Not built yet |

Nothing in this folder touches AWS unless you run a script in `deploy/` yourself.

## What is here

```
sealbox/
  envelope.py   lock/unlock weight files chunk by chunk, each chunk tamper-checked
  jobform.py    accepts a fine-tuning job form only if every field is inside limits
  manifest.py   the list of what the sealed machine may load, and its fingerprint
  cli.py        command-line tool for the three modules above
deploy/
  guard.sh      stops unless the logged-in AWS account is the one you named
tests/          56 automated tests
```

## How each piece maps to the gaps in the document

| Piece | Gap it closes | What changes |
| --- | --- | --- |
| `envelope.py` | C1 (RAM limit), A8 (no tamper check) | Memory use stays flat as the model grows. Changed, reordered or cut-off files are refused. |
| `manifest.py` | A3 (customer picks where code loads from) | The bucket, the key and each container fingerprint are fixed in one list with one fingerprint. |
| `jobform.py` | C2 (fine-tuning) | The customer sends a form, never code. Unknown fields and out-of-limit settings are refused. |
| `guard.sh` | Safe operation | Deploys run only in the account you name. The default profile is never used. |

## Try it

```bash
pip install -r requirements.txt
python -m pytest -q

python -m sealbox.cli new-key --out test.key        # local testing only
python -m sealbox.cli lock   --key test.key --in weights.bin  --out weights.sbox
python -m sealbox.cli unlock --key test.key --in weights.sbox --out weights.out
```

The key file is for local testing only. On AWS the key comes from KMS and is
never written to disk.

## Before any AWS step

Run these in your own terminal, with the account you chose for this POC:

```bash
export POC_ACCOUNT_ID=<12-digit account id>
export POC_REGION=<region>
export AWS_PROFILE=<profile name>
bash deploy/guard.sh
```

It prints `OK` only when the logged-in account matches `POC_ACCOUNT_ID`.

## Next

1. Key release: the machine proves itself and KMS returns the key (needs the account).
2. Sealed image, based on the AWS sample with the open access paths removed.
3. vLLM serving with chunked loading, then the sealed trainer.

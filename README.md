# Sealed model hosting

Run a model inside a customer's own AWS account, without the customer being
able to read or copy the model's weights. The customer's data never leaves
their account, and they can still fine-tune the model on private data.

This is a proof of concept built at zeb. It is not a product yet. Everything
below says plainly what was run and what was not.

- **How to use it, step by step:** [`docs/user-guide.md`](docs/user-guide.md). Setup, choosing and
  releasing a model, bringing up a customer, the endpoint, fine-tuning, and every option.
- **What is original and what we changed:** [`docs/changes-vs-original.md`](docs/changes-vs-original.md).
- **One page that explains it all:** open `docs/flow-explainer.html` in a browser.

## The idea in five lines

1. The model builder encrypts the model. The key stays in the builder's AWS key service (KMS).
2. The builder ships a sealed machine image: no login, read-only disk, one fixed job.
3. At boot the machine proves to KMS, with a proof signed by AWS hardware, exactly which image it booted.
4. KMS releases the key only for that exact image, once per boot. Then the machine locks itself.
5. The builder can switch a customer off at any time. The machine stops within about two minutes.

## Where it stands (24 November 2026)

| What | Result |
| --- | --- |
| Attack suite | 32 of 33 tests pass on release 0.10.0. The one left is a known open item (reproducible build). The current release, 0.10.1, is the same code built again; the full suite has not run on it. |
| One-click customer setup | Eight tests pass: one stack brings up a working, separate customer. |
| Cut-off | Answers stop 80 to 120 seconds after the keys are disabled. |
| Spend so far | About 70 USD of a 500 USD limit. |

**27 November:** the remediation agent now runs on AWS AgentCore next to the release agent, and `bash smh.sh setup` sets up the builder's side with one command (`docs/m12-plan.md`). Release 0.12.0 was made and served with them; the full suite has not run on it.

Not shown, said plainly:

- **One AWS account.** Builder and customers are separate sets of resources in it. The real wall needs a second account.
- **The one-click template's normal path** (it creates its own network) was not run: the test account is at its limit of five networks. The Launch Stack link was produced, not clicked.
- **The release approval** is given in advance and applied when six automatic checks pass. A human reading each release before approving it is not shown.
- **The build is not reproducible yet**, so a customer cannot rebuild the image and compare.
- Secure Boot uses test keys. One small open model on one GPU.

Full results: `docs/results-summary.md`, `docs/m9-plan.md`, `docs/m10-plan.md`.
Full history: `docs/process-report.md`.

## How to use it: one script

**The full guide, end to end:** [`docs/user-guide.md`](docs/user-guide.md). It covers setup,
choosing and releasing a model, bringing up a customer, using the endpoint,
fine-tuning, every menu entry and command, and what to do when something goes wrong.

There is one script for everyone. You need a laptop that is signed in to the
project's AWS profile (ask the owner).

### The two commands most people need

```bash
bash smh.sh ship        # BUILDER: prepare a release, ask for your approval, tie the keys, give the customer access
```

```bash
bash smh.sh up          # CUSTOMER: network, sealed machine, API key and a test call, in one go
```

`ship` takes an open model by name (`bash smh.sh ship owner/name`) or your
own model from a folder (`bash smh.sh ship /path/to/folder`; a disk, a
network share or `s3://`). It takes about 1 hour. It asks one question in the first second (approve
by itself when every check passes?) and then needs nothing more; the screen
shows one status line a minute. `up` takes about 25 minutes and ends with the endpoint address
and how to call it.

### An AI agent watches every run

Each step of `ship`, `up` and `finetune` runs under a watcher. When a step
fails, an AI agent looks into it with read-only tools (stack events, the
machine's log, key rules, your AWS permissions, account limits, facts about
the model). It then either applies a fix from a short fixed list and runs the
step again, or tells you exactly what to do and continues when you say it is
done. It works the same on the builder's side and the customer's side.

**Before first use**, copy `smh.env.example` to `smh.env` and replace
`REPLACE_WITH_YOUR_BEDROCK_INFERENCE_PROFILE` with a Claude inference profile
your AWS account may use. Details: section 0 of the user guide.

### Or let it guide you

```bash
bash smh.sh
```

It asks who you are (model builder, customer, or demo and tests), then shows
a short menu. The top of each menu shows the current release, what is
running, what has been spent, and the **suggested next step**. Every entry
that starts a machine or changes a key asks first and says the cost.

To look without touching anything, use show-only mode. It prints what each
entry would run:

```bash
DRY=1 bash smh.sh
```

### Every action is also a command

```bash
bash smh.sh help
```

| I want to... | Menu | Command |
| --- | --- | --- |
| Do the builder's whole job | Builder 1 | `bash smh.sh ship [model]` |
| Approve a release that is waiting | Builder 2 | `bash smh.sh approve <version>` |
| Give a customer access, or take it away | Builder 3, 4 | `bash smh.sh onboard`, `offboard` |
| Stop every customer, or allow them again | Builder 6, 7 | `bash smh.sh cutoff`, `restore` |
| Do the customer's whole job | Customer 1 | `bash smh.sh up` |
| Check the endpoint from inside the network | Customer 2 | `bash smh.sh test` |
| Teach the model something new from a file | Customer 3 | `bash smh.sh finetune <file.jsonl>` |
| Make a new API key | Customer 4 | `bash smh.sh api-key` |
| Keep the model in the customer's own bucket | Customer 5 | `bash smh.sh copy-model` |
| Remove the customer's side | Customer 8 | `bash smh.sh remove` |
| Prove the protections still hold | Builder 8, Demo 6 | `bash smh.sh suite static`, `suite all` |
| Run the ten-point demo | Demo 1 to 5 | `bash smh.sh demo prepare`, then `demo 1` to `demo 10` |
| See what is running and what was spent | Demo 7 | `bash smh.sh status` |
| Turn everything off at the end of the day | Demo 8 | `bash smh.sh off` |

Remove the customer's side before the suite or the demo: in our account they
share one network slot.

### A full round, from nothing running

```bash
bash smh.sh ship                                   # builder: about 1 hour; one question at the start
bash smh.sh up                                     # customer: about 25 minutes; ends with the endpoint
bash smh.sh finetune image/m4/sample/data.jsonl    # customer: train and serve an adapter
bash smh.sh remove                                 # customer: delete everything of theirs
```

### Check that the code works (no AWS needed)

```bash
cd sealed-model-poc && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 180 tests
cd ../assure && ../sealed-model-poc/.venv/bin/python -m pytest -q .   # 39 tests
cd ../agent && ../sealed-model-poc/.venv/bin/python -m pytest -q .    # 6 tests (the watcher's safety net)
```

### What the script uses underneath

You do not need to call these yourself.

| File | What it does |
| --- | --- |
| `assure.sh`, `assure/` | The attack suite and its checks. |
| `demo.sh` | The ten demo points. |
| `deployer/` | The release agent (AWS AgentCore) and the approval step. |
| `infra/` | The CloudFormation templates and their deploy script. |
| `eod.sh` | Lists every resource this project owns. |
| `check-access.sh` | Read-only check of what the AWS user may do. |
| `agent/` | The watcher: `fixer.py` and its tests. |
| `smh.env.example` | Template for your settings. Copy it to `smh.env` (not committed) and fill in the watcher's model. |

### Where things are

| Folder | Contents |
| --- | --- |
| `sealed-model-poc/sealbox/` | The code that runs on the sealed machine: encryption format, loader, weights proxy, job runner, lease, usage meter, log shipper, API keys. |
| `image/` | The recipe for the sealed image and the build pipeline. |
| `infra/m1/` | CloudFormation templates and scripts for the builder side and the first customer. |
| `infra/oneclick/` | The customer's one-click template. |
| `infra/cost-guard/` | Tracks spend and stops our machines every night and near the budget. |
| `deployer/` | The release agent and its fixed tools. |
| `assure/` | The suite's checks, collector and report writer. |
| `docs/` | Problem, design, plans and results for each milestone, the log, and the resource ledger. |
| `evidence/` | Raw output of every run. Each claim marked "Run" points here. |

## Rules that keep this safe

These are binding for anyone (or any assistant) working in this repository.
The full list is in `CLAUDE.md`.

- Use only the AWS profile `poc-ino` and account `111122223333`, region `us-east-1`. Every script checks this and stops otherwise.
- Ask the owner before creating, changing or deleting anything in AWS, and before anything that costs money.
- Every resource is named `smh-poc-*`, carries the six required tags, and gets a line in `docs/resources.md`.
- Touch nothing that the project did not create. The account is shared.
- Never commit a key or a secret. `.gitignore` leaves out raw CloudTrail captures because they hold access key IDs.
- Turn everything off at the end of the day (`bash smh.sh off`).

## Words used for evidence

| Label | Meaning |
| --- | --- |
| Run | We executed it and have the output. |
| Read in code | We saw the exact lines. |
| Inferred | Assembled from separate pieces of code. |
| Hypothesis | Follows from documentation. |

Nothing is called proven unless it is "Run".

## Using it in another AWS account

Not tried. The account number, profile name, region and the `smh-poc-` prefix
are written at the top of each script and in the templates. A second account
for the customer side is the next real step; see "Next" in
`docs/process-report.md`.

## Reference material

- AWS sample this builds on: `aws-samples/sample-guarded-user-controlled-attested-runtime-deployment`,
  commit `b82acf76e4650f26e87e063fad1d643ca1101c57`. Clone it into `vendor/guardian` if you need it.
  Treat it as untrusted: read it, do not run its scripts against the account.

## About this copy

This is a shared copy. Internal working documents that the text above may name (milestone plans, the run log, the resource ledger, raw evidence of each run) are not part of it, and neither is the test file of the attack suite (`assure/test_checks.py`), because it replays that evidence. The account number `111122223333`, the email `owner@example.com` and the user name `your-iam-user` are placeholders: search for them and put in your own.

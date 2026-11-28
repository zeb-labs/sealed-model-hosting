# User guide: sealed model hosting, end to end

This guide covers setup, choosing and releasing a model, bringing up a
customer, using the endpoint, fine-tuning, and every option of the tool.

One script does everything. Most people need two commands:

```bash
bash smh.sh ship         # the BUILDER's whole job: release, your approval, keys, customer access (section 3)
bash smh.sh up           # the CUSTOMER's whole job: network, machine, API key, test call (section 5)
```

Or let it guide you:

```bash
bash smh.sh              # asks who you are, then shows a short menu with the suggested next step
bash smh.sh builder      # straight to the builder's menu (also: customer, more)
DRY=1 bash smh.sh        # show-only mode: prints what each entry would run
bash smh.sh help         # the list of commands
bash smh.sh <command>    # one action, straight from the command line
```

Menu entries are written below as "Builder 1", "Customer 3" or "Demo 6":
the menu, then the number in it.

Words used here:

- **Builder**: the party that owns the model.
- **Customer**: the party in whose AWS account the model runs.
- **Sealed machine**: the GPU machine that runs the model. Nobody can log in to it.
- **Release**: one model, encrypted, plus the sealed image that may open it.
- **Adapter**: the small add-on file that fine-tuning produces. The model itself does not change.

Each claim below is marked **Run** (we executed it) or **Not run**.

---

## 0. The watcher: an AI agent that looks after every run

You do not have to understand an error to get past it. Every step of
`ship` (builder) and `up` (customer), and `finetune`, runs under a watcher.

When a step fails, the watcher:

1. **Looks into it.** An AI agent (Claude, on Amazon Bedrock) reads the step's
   output and investigates with tools that can only read: why a stack failed,
   what the sealed machine logged, what the keys are tied to, what your AWS
   user is allowed to do, account limits, facts about the model.
2. **Fixes it, if it is safe.** It picks one fix from a short fixed list, the
   script carries it out, and the step runs again.
3. **Otherwise tells you exactly what to do**, in numbered steps, then waits.
   Press Enter when it is done and the run continues from that step.

| The watcher can do by itself | Side |
| --- | --- |
| Run the step again, or wait and run it again (for example: no free GPU machine) | both |
| Try another GPU machine type | customer |
| Delete its own half-made stack from a failed try | customer |
| Restart the sealed machine | customer |
| Make a new API key | customer |
| Give the customer's machine role access on the keys | builder |

Everything else goes to a person: a missing AWS permission (it names the
exact actions to ask your administrator for), an account limit, a model that
cannot be released (gated, no safetensors weights, too big), a customer who
has no access yet (it gives the exact words to send the builder).

What keeps it safe:

- It can only **look**. None of its tools changes anything.
- It cannot run commands. It chooses from the list above; the script owns the code of each fix.
- A fix that the side is not allowed to do becomes a request to a person.
- It never proposes turning a protection off or widening a role.
- Every case is saved in `evidence/watcher/`: what failed, what it looked at, what it decided.

**Where it runs.** The watcher is an agent on AWS AgentCore (harness
`smh_poc_watcher`), separate from the release agent on purpose: text from a
log can reach the watcher, and the watcher can change nothing. Its tools sit
behind its own gateway, in a function whose role can only read. Each decision
is also written to the builder's bucket under `deploy/watch/`, and every tool
call to the function's log. If the harness does not answer, the same
investigation runs from the laptop with the same model, and if the model
cannot be reached, plain rules answer.

**Setting it up (required once).** The watcher needs to know which Claude
model on Bedrock to use. Copy `smh.env.example` to `smh.env` and replace the
placeholder with an inference profile your account may use:

```bash
cp smh.env.example smh.env        # then edit SMH_AGENT_MODEL in it
```

Nothing is created for it. Without the setting, the watcher still explains
failures, using plain built-in rules instead of the AI model. To switch the
watcher off for one run: `SMH_NO_WATCHER=1 bash smh.sh up`.

## The control page: the same actions as buttons

You do not need the terminal after the first line:

```bash
bash smh.sh ui          # then open http://127.0.0.1:8765 in your browser
```

The page has five tabs: Builder, Customers, Models, Customer side, House. Each button runs one
fixed command of `smh.sh`; an agent on AgentCore does the work. On the right
you see what the agent is doing, how long it has run, and, if a step fails,
the remediation agent's finding and the exact steps for you. Actions that
start paid machines are marked and ask once before they start.

| On the page | The command behind it |
| --- | --- |
| Set up the builder's side | `bash smh.sh setup` |
| Ship a model | `APPROVE=yes bash smh.sh ship <model>`: approved by itself only when all six checks pass |
| Give or take the customer's access | `onboard`, `offboard` |
| Bring up the customer's side, test, fine-tune, restart, remove | `up`, `test`, `finetune`, `restart`, `remove` |
| Run the full attack suite (about 4.5 hours) | `suite all` |
| Refresh status, switch everything off, emergency stop for everyone | `status`, `off`, `cutoff`, `restore` |

The page also notices long work started from a terminal (the attack suite, a release, a bring-up). While that runs it says so and refuses every button that would change something; Refresh status still works.

What keeps it safe:

- It listens on your own machine only, and uses the AWS login the laptop already has. It adds no new rights.
- A random token is made at each start; another web site open in your browser cannot press the buttons.
- No input reaches a shell. A model name, a folder and a data file are each checked against a pattern.
- One job at a time.

It needs Python 3 and nothing else. Check it: `python3 ui/server.py --test`.

### Hosting it for a team (later; not run)

The same two files (`ui/server.py`, `ui/index.html`) can run on a small AWS
machine instead of a laptop. This was NOT run. The page can start GPU
machines, so it must never be reachable without a login.

1. **Where.** A small EC2 instance (for example `t3.small`) in a private subnet, with this repository checked out, the AWS command line, and an instance role that has what your own user has today for `smh.sh`. A container on EKS or ECS works the same way; one copy only, because the running job is kept in memory.
2. **A login in front.** An Application Load Balancer with HTTPS and its built-in sign-in (Amazon Cognito or your company's single sign-on) in front of the instance. Allow only the load balancer to reach port 8765.
3. **Start it** with the three settings that say you have done step 2:

   ```bash
   SMH_UI_HOST=0.0.0.0 SMH_UI_NAMES=smh.example.com SMH_UI_BEHIND_LOGIN=yes python3 ui/server.py
   ```

   Without `SMH_UI_BEHIND_LOGIN=yes` the server refuses any address but the machine itself.
4. **Simplest alternative, no load balancer:** keep it listening on the instance only and reach it through a Session Manager port forward: `aws ssm start-session --target <instance> --document-name AWS-StartPortForwardingSession --parameters portNumber=8765,localPortNumber=8765`. Each person then opens `http://127.0.0.1:8765` and AWS has already checked who they are.
5. **Before real use:** who pressed what is not recorded by the page (the agents' own records are); add that, and keep the approval of a release with a person, not with the page.

## 1. What you need before you start

| Need | Detail |
| --- | --- |
| A Mac or Linux laptop | Kept awake and on mains power for anything longer than a few minutes |
| AWS command line (`aws`) version 2 | Signed in to the profile `poc-ino`, account `111122223333`, region `us-east-1` |
| Python 3.10 or newer | For the offline tests and the helper scripts |
| `bash`, `openssl`, `git` | Already on most laptops |
| GPU quota in the account | 4 vCPUs is enough for one sealed machine; we have 768 |
| Access to a Claude model on Amazon Bedrock | For the watcher (section 0); set `SMH_AGENT_MODEL` in `smh.env` |

Check the laptop once:

```bash
aws sts get-caller-identity --profile poc-ino     # must show account 111122223333
cd sealed-model-poc && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                     # 180 tests must pass
cd ..
bash smh.sh status                                # shows the release, what is running, the spend
```

Every script stops by itself if the credentials are for another account than `SMH_ACCOUNT` in `smh.env` (default: ours, `111122223333`).

## 2. First-time setup of the builder's side

One command:

```bash
cp smh.env.example smh.env      # once: your account, profile, region, owner tag and the agents' model
bash smh.sh setup
```

It is safe to run again: what exists is left alone. It does three things.

**1. Creates the fixed pieces that are missing** (CloudFormation, with your own AWS user):

| Piece | Stack | What it is |
| --- | --- | --- |
| Cost guard | `smh-poc-cost-guard` | Spend tracking, an email alert, a nightly stop and a stop near the budget |
| Base | `smh-poc-m1-base` | The builder's network and bucket, the machine roles |
| Keys | `smh-poc-m1-key`, `smh-poc-m1-lease` | The model key and the lease key. They release nothing until a release is approved. |
| First customer | `smh-poc-m1-customer` | A customer of your own, for tests and the demo |
| Build machine | `smh-poc-m1-builder` | Stopped when idle |

**2. Sets up the three agents on AWS AgentCore** (`deployer/setup.sh`): the release
agent, the watcher, and the customer agent that `bash smh.sh up` hands its work to
(`SMH_UP=script bash smh.sh up` does the same steps without the agent). Each is a harness that may call only its own gateway.
Both think with the Claude inference profile you named; nothing is created for it.

**3. The release agent prepares the build machine.** It checks that every piece
is there, starts the machine, runs ONE fixed script on it
(`image/pipeline/bootstrap-builder.sh`), reads back the result and stops the
machine. The script installs the build tools from the Amazon Linux repository,
makes the Secure Boot keys, builds the NVIDIA driver for the kernel the image
will get and signs it, downloads and verifies the driver packages, and makes
the lease secret. It never replaces a key or the lease secret.

**Your own Secure Boot keys.** Before the first run, put `PK.key PK.crt KEK.key
KEK.crt db.key db.crt` into `/opt/smh/sb` on the build machine (root only).
Whatever is missing there is made as a TEST key, and the agent says so.

**If a step fails**, the watcher looks into it and tells you what to do, as for
every other command (section 0).

What was run and what was not is in `docs/m12-plan.md`. In short: in our
account every piece already existed, so the command was run as a check and a
repair, and the bootstrap script was run on a fresh second build machine. A
whole empty account in one go was not run.

Rules that always apply in our own account (the full list is in `CLAUDE.md`):

- Every resource is named `smh-poc-*` and carries the six required tags.
- IAM roles use the path `/zl/`.
- Every new resource gets a line in `docs/resources.md`.
- The account is shared. Touch nothing this project did not create.

## 3. The builder: choose a model and release it

### 3.1 Which models can be used

Two sources work:

- **An open model on Hugging Face**, by name: `bash smh.sh ship owner/name`.
- **Your own model**, from a folder: `bash smh.sh ship /path/to/folder`. The
  folder can be on this disk, on a mounted network share, or an `s3://`
  location you can read. This is the route for a model you trained yourself.

Your own model must be a Hugging Face style folder, the kind
`save_pretrained(..., safe_serialization=True)` writes:

| File | Needed |
| --- | --- |
| `config.json` | Yes |
| `*.safetensors` (the weights) | Yes |
| tokenizer files (`tokenizer.json`, `tokenizer_config.json`, ...) | Yes |

What happens to your own weights: they are copied into the builder's own
bucket, the release encrypts them under a fresh key, and the copy is deleted
as soon as the encryption is done. They never go to Hugging Face or anywhere
else.

A gated or private Hugging Face model cannot be fetched by name (the build
has no token). If you hold its weights, ship them from a folder instead.

In both cases the release agent checks the model and picks a GPU machine by
itself.

| Rule | Why | Status |
| --- | --- | --- |
| By name: the model must be public on Hugging Face (no login, no licence click). From a folder: no such rule. | The build downloads with no token | Run (a gated model was refused and explained by the watcher) |
| It must have `.safetensors` weight files | The agent refuses anything else | Read in code |
| It must be a text model that vLLM can serve and load in the tensorizer format | The sealed machine runs vLLM unmodified | Run for one model only |
| Loading needs about twice the model's size in GPU memory, plus 2 GiB | Measured: a 14.5 GiB model failed on a 22 GiB GPU | Run |
| So the weights must be at most about 11 GiB for a 24 GiB GPU, or about 23 GiB for a 48 GiB GPU | The agent's machine table: `g5.xlarge`, `g6.xlarge`, `g5.2xlarge` (24 GiB), `g6e.xlarge` (48 GiB) | Read in code |
| Fine-tuning expects layers named `q_proj` and `v_proj` | The job form names them | Run for one model only |

**Two models have been released, served and fine-tuned:
`Qwen/Qwen2.5-1.5B-Instruct` by name (3.1 GB), and a Llama-architecture
model (SmolLM2-360M, 0.7 GB) from a folder, as a stand-in for a self-trained
model.** Any other model is untested. A larger model may also need the
GPU memory share in `image/m5/loader.json` (`--gpu-memory-utilization`,
now `0.45`) raised; that has not been tried.

### 3.2 Release it

The easy way, **Builder 1**, or:

```bash
bash smh.sh ship                    # the default model
bash smh.sh ship <owner/model>      # another model
```

`ship` asks **one question, in the first second**: "Approve this release by
itself when all its checks pass?" Type `yes` and you are finished; nothing
else is asked. It then picks the next version number and a fresh folder,
lets the agent prepare the release, approves it if every check says PASS,
ties the keys to the image, and gives the customer access (section 4).

- While it works, the screen prints one status line a minute: what it is
  doing, how far along, and when it will be done. There is nothing to type.
- If any check does not pass, it does **not** approve. It stops and waits
  for your decision (section 3.3).
- If you answer anything other than `yes` at the start, it asks you at the
  end instead, with a sound, a notification and a banner.
- `APPROVE=yes bash smh.sh ship` skips even the first question.
- If no GPU machine is free it waits 10 minutes and asks again, up to six times.
- The agent's full working notes are in `evidence/releases/agent-<version>.txt`.

What "approve by itself" means: nobody reads the release summary before the
keys are tied to the image. The six automatic checks decide. If a person
must read and approve every release, use the single steps below.

To choose the version and folder yourself, use the single steps:

```bash
bash smh.sh release <owner/model> <version> <folder>
# example
bash smh.sh release Qwen/Qwen2.5-1.5B-Instruct 0.10.1 m13
```

| Value | Rule |
| --- | --- |
| `version` | Three numbers, like `0.10.1`. Each release needs a new one. |
| `folder` | Starts with `m`, lowercase letters, digits or `-`. Must be empty: each release gets a fresh folder and a fresh encryption key. |

What happens, with no further input (about 55 minutes, about 1.50 USD):

1. The agent measures the model and picks a GPU machine.
2. A temporary GPU machine prepares and encrypts the weights and the model-server container (about 25 minutes).
3. The build machine builds and signs the sealed image and computes its four measurements.
4. The image is registered and checked (no login packages, right registration, right roles and network).
5. An install package for customers is written.
6. The agent stops and prints `NEEDS_APPROVAL` with a summary.

The transcript is saved in `evidence/releases/agent-<version>.txt`.

### 3.3 Approve it

Done by `ship` when you agreed at the start. As a human decision on its own: **Builder 2**, or:

```bash
bash smh.sh approve <version>
```

The script shows the image, its measurements and the check results, compares
them with the build's own output file, plays a sound, shows a notification
and a banner, and asks you to **type `yes`**. Anything typed before the
question appears is thrown away, so an early `yes` cannot approve. Then the agent ties
both keys to the new image. From that moment the previous image can no
longer get the key.

The build's outputs land in `evidence/releases/build-<version>/`. The tool
then treats this release as the current one.

### 3.4 Prove the release (optional, recommended)

**Builder 8** (read-only) and **Demo 6** (everything), or:

```bash
bash smh.sh suite static      # read-only checks, free, about 1 minute
bash smh.sh suite all         # all 33 tests, about 4.5 hours, about 5 USD
```

Before `suite all`:

- Remove the one-click customer (`bash smh.sh remove`). In our account both use one network slot.
- The two tampered test images must exist for tests B2 and B3. On the build machine run `PREFIX=<folder> image/pipeline/build-breaks.sh`, then on the laptop `bash infra/m1/publish-breaks.sh evidence/releases/build-<version>`. Without them those two tests are reported as NOT RUN.
- Keep the laptop awake and on mains power.

The report is `evidence/releases/suite-<version>/report.md`. Every PASS
names the raw file it was judged from.

## 4. The builder: allow a customer

Each customer's machine has its own role. The builder adds that one role to
the rules of both keys. The four measurements are still required; the role
alone gets nothing.

Part of `ship`. On their own: **Builder 3** and **Builder 4**, or:

```bash
bash smh.sh onboard                 # the one-click customer of this account
bash smh.sh onboard <role ARN>      # any other machine role
bash smh.sh offboard                # take it away again
```

- Onboarding can be done **before** the customer has created anything.
- One customer can be onboarded at a time in this proof of concept.
- `offboard` cuts that one customer off. Their machine stops within about 4 minutes (Run). Other customers are not touched.
- A new release keeps the onboarded customer (Run).

## 3a. The life cycle: where the image comes from, and what lands in the customer's account

The same content is the first tab of the control page (`bash smh.sh ui`).

**A. The builder makes a release** (`bash smh.sh ship`)
- The release agent encrypts the model and puts the files in the builder's S3 bucket, one folder per release.
- The build machine builds the sealed image from the recipe, with the fingerprints of those files inside, and signs it.
- The image is registered as an AMI (an Amazon machine image) **in the builder's account**. Both keys are tied to that image's measurements.

**B. The builder onboards a customer** (`bash smh.sh onboard-customer <name> <account>`)
- The AMI is **shared** with the customer's AWS account. It is not copied. It holds no model and no key.
- The customer's machine role may read that one release folder in the builder's bucket. The files are ciphertext.
- The keys are **never shared**. The key policies name the customer's machine role and still require the image's measurements.
- An install kit is written: image ID, key ARNs, bucket name, settings.

**C. The customer configures their AWS account** (`bash smh.sh customer-setup <kit>`, once)
- A small private bucket for the agent's own files.
- The customer agent and the remediation agent on AgentCore, in the customer's own account.

**D. The customer brings it up** (`bash smh.sh up`)
- The customer agent creates ONE CloudFormation stack (`infra/oneclick/customer.yaml`). It holds all the supporting runtime:

| Created in the customer's account | What for |
| --- | --- |
| A private network, no internet, with three private endpoints (S3, KMS, logs) | The machine can reach storage, the key service and the log, and nothing else |
| The customer's own S3 bucket | Their data, fine-tuned adapters, API keys, usage records |
| The customer's own KMS key | Protects everything in that bucket |
| A log group | The machine's log, readable by the customer |
| The machine's role and profile | The role the builder named on the keys |
| The sealed machine | Started from the shared AMI |
| An internal load balancer | The endpoint, with a name that survives restarts |

- The agent then makes an API key and tests the endpoint.

**E. Every boot, by itself**
- The machine proves to KMS what it booted, gets the model's key once, locks itself, and streams the encrypted model to the GPU.

**F. Running, and the end**
- The customer calls the endpoint and fine-tunes. The builder reads usage counts, and can pause, resume or remove that one customer.
- A new release: `ship` again; each customer's machine loads it at its next restart.

## 4. The full cycle for one customer, from onboarding

| # | Who | Step | Command | On the page |
| --- | --- | --- | --- | --- |
| 1 | Builder | Onboard: register the customer by AWS account number, give access on both keys, share the release, write the install kit | `bash smh.sh onboard-customer acme 111122223333` | Customers tab, "Onboard a new customer" |
| 2 | Builder | Send the folder `kits/acme/` to the customer. It holds no secret | | |
| 3 | Customer | Configure their own AWS account, once: the agent's bucket, the install package, the customer agent and the remediation agent on AgentCore | `bash smh.sh customer-setup <kit folder>` | Customer side tab, "Configure this AWS account" |
| 4 | Customer | Bring up: one stack, the sealed machine proves itself and loads the model, an API key, a test | `bash smh.sh up` | Customer side tab, "Bring up the customer's side" |
| 5 | Both | Run: the customer calls the endpoint and fine-tunes; the builder reads usage counts | `finetune`, `usage` | Customer side and Builder tabs |
| 6 | Builder | Pause, resume or remove that one customer | `bash smh.sh customer pause acme` | Customers tab, the customer's row |

How step 1 can come before the customer has installed anything: the customer's
stack is named `smh-poc-oc-<name>`, and its machine role
`role/zl/smh-poc-oc-<name>-sealed-role` in their account. So the role is known
from the name and the account number alone.

What "share the release" does: the active customers' machine roles may fetch
the objects of the live release's folder in the builder's bucket (ciphertext
only; stack `smh-poc-m1-sharing`), and, for a customer in another account, the
image is shared with that account.

The kit (`kits/<name>/`): `install.json` (image, the two key ARNs, the
builder's bucket), a filled-in `smh.env` (their account, their stack's name),
and a `README.md` with the four steps. The customer adds three things to
`smh.env`: their AWS profile, their owner tag, and a Claude inference profile
on Amazon Bedrock that their account may use.

**Said plainly:** we have one AWS account. Steps 1 to 6 were run with the
customer in the SAME account as the builder (see `docs/m12-plan.md`). The
parts that only matter across two accounts (sharing the image, the bucket
rule, the customer's own agent bucket) are written and were not run.

## 4a. Many customers, and many releases

A builder with ten customers manages each one alone.

```bash
bash smh.sh customers                                  # the list: name, state, account, since when, machine role
bash smh.sh customer add acme arn:aws:iam::111122223333:role/zl/acme-sealed-role
bash smh.sh customer pause acme                        # only acme stops, within about four minutes
bash smh.sh customer resume acme
bash smh.sh customer remove acme
```

- A customer is a name and the ARN of the role their sealed machine runs as. Their stack prints it; it may be in another AWS account.
- The list is one small file in the builder's bucket (`deploy/customers/registry.json`).
- Only ACTIVE customers are written into the two key policies, in one statement, each under the same measurements. A role alone still gets nothing.
- Pausing one customer removes only that role. Its machine can no longer renew its lease and stops; the others never notice.
- `bash smh.sh cutoff` (disable both keys for everyone) is kept for an emergency only.
- Limit today: about 45 active customers (one CloudFormation parameter). Beyond that the policy must be written from the list directly.

Releases:

```bash
bash smh.sh models            # every release: model, state (live, available, not usable), image
bash smh.sh live 0.10.5       # tie the keys to an older approved release again
```

One release is live at a time: the keys are tied to its image, and every customer runs it. Different customers on different models at the same time is not built; it needs one set of measurements per release in the key policies.

On the control page these are the **Customers** and **Models** tabs, with a button per row.

What was run is in `docs/m12-plan.md`.

## 5. The customer: install with one stack

### 5.1 What the stack creates

One CloudFormation template, `infra/oneclick/customer.yaml`:

- a private network with no route to the internet;
- three private doors to AWS services: storage (S3), keys (KMS), logs;
- the customer's own bucket, key and log group;
- the machine's role, with no admin rights;
- the sealed machine;
- an internal load balancer, so the endpoint address stays the same when the machine is restarted or replaced.

### 5.2 Install

The easy way, **Customer 1**, or:

```bash
bash smh.sh up
```

`up` creates the stack, waits for the model, makes an API key if there is
none, runs a test call, and prints the endpoint address and how to call it.
The single steps it runs are `install`, `api-key` (section 5.3) and `test`
(section 5.4):

```bash
bash smh.sh install
```

It takes about 20 minutes: 5 for the stack, about 12 for the machine to
fetch, check and load the model. It ends with the endpoint address.

- If a GPU type has no free capacity, the tool tries `g5.xlarge`, then `g6.xlarge`, then `g5.2xlarge`, and cleans up a failed first try by itself.
- `TYPE=g6.xlarge bash smh.sh install` forces one type.
- `NAME=acme bash smh.sh install` uses another name prefix (default `smh-poc-oc`). The builder must onboard `role/zl/<NAME>-sealed-role`.

The same template can be launched from the AWS console:

```bash
bash smh.sh link        # Customer 7: prints a Launch Stack link
```

**Not run:** nobody has clicked that link. In our account the console wizard
also needs the six tags typed on its tag page.

**Not run:** the stack creating its own network. Our account is at its
limit of five networks, so `smh.env` makes the stack build inside our
existing customer network. Leave the `OC_` lines out of `smh.env` to use the normal path.

### 5.3 Make an API key

Part of `up`. On its own: **Customer 4**, or:

```bash
bash smh.sh api-key
```

The endpoint refuses every model request until a key exists. This command
makes a random key, saves it on the laptop in `~/.smh-poc/<NAME>-api-key`,
and puts it in the customer's bucket at `gate/api-keys.txt`. The machine
reads that file every 30 seconds.

To manage keys by hand, edit that file: one key per line, 24 to 128
letters, digits, `-` or `_`, at most 50 keys. Remove a line to withdraw a
key. The key is never printed by the tool.

### 5.4 Check the endpoint

The endpoint can be reached **only from inside the customer's network**.
Part of `up`. On its own: **Customer 2**, or:

```bash
bash smh.sh test              # ask the base model
bash smh.sh test <adapter>    # also ask one adapter
```

This starts a small stand-in app inside the network and prints what it saw:
health, the certificate fingerprint, that a call with no key is refused
(401), which ports are open (only 443), and an answer.

### 5.5 Keep the model in the customer's bucket (optional)

By default the machine loads the encrypted release from the builder's
bucket. To remove that dependency, **Customer 5**, or:

```bash
bash smh.sh copy-model
```

It copies about 15 GB of encrypted files into the customer's bucket and
restarts the machine from that copy (about 25 minutes). The copy is
unreadable without the builder's key.

## 5a. Using the model: two ways

Once the customer's side is up, the sealed model can be used in two ways. In
both, the customer never sees the model's weights.

**Way 1: direct inference, for your own applications.** Any program inside
the customer's network calls the endpoint over HTTPS with an API key. The
endpoint speaks the standard chat API (the format OpenAI uses), so existing
libraries and tools work. Section 6 has the details.

**Way 2: through AWS AgentCore.** An agent on AgentCore Runtime, in the
customer's own account and network, whose model is the sealed model:

```bash
bash smh.sh agent-up                      # about 5 minutes
bash smh.sh ask "What is 17 times 23?"    # the answer comes from the sealed model, through AgentCore
bash smh.sh agent-remove
```

On the control page: Customer side tab, "Use the model through AgentCore".

What `agent-up` does:

1. Reads the sealed machine's certificate fingerprint from the machine's own log.
2. Makes the agent's role (it may read its own code and the customer's API key file, nothing else) and its security group (stack `<name>-agent`).
3. Uploads the agent's code package and creates the AgentCore Runtime in VPC mode, inside the customer's subnet.

How the agent stays safe:

- It calls the endpoint like any app. It has no key of the model and cannot read the weights.
- It accepts exactly one certificate, the sealed machine's, by fingerprint. A different certificate is refused before anything is sent.
- It reads the customer's own API key file itself; the key is never printed.
- Its two demonstration tools (a calculator and a clock) take no free text that is executed.

After the sealed machine restarts it makes a new certificate: run `bash smh.sh agent-up` again.

The agent's code is one file, `consume/main.py`. It is a starting point: replace
the two demonstration tools with your own.

**A harness instead of code** (`consume/harness.py`) is a second form of way 2:
an AgentCore harness whose model setting points at the sealed endpoint through
a private API Gateway in your network. Run on 28 Nov: it answered with the
sealed model.

    python consume/harness.py up <your stack>        about 25 minutes
    python consume/harness.py ask <your stack> "<question>"
    python consume/harness.py remove <your stack>

Know before you use it:

- The gateway does not check who signed the sealed machine's certificate (it
  is self-signed). The hop stays inside your private network. A certificate
  from a public authority removes both the gateway and this limit.
- It needs six extra private endpoints. Do not leave them in the sealed
  machine's network: the release check S7 refuses a network that can reach
  more than storage, the key service and logs. Give the harness its own network.
- After the sealed machine is replaced its address changes: run `up` again.
- Tool use through the harness was not tried. It is not in `smh.sh` or the page.

## 6. Using the endpoint from your own app

Address: the `Endpoint` output of the stack (`bash smh.sh status` shows it).

| Path | Needs a key | Purpose |
| --- | --- | --- |
| `GET /health` | No | Is the model up |
| `GET /v1/models` | Yes | The base model (`demo`) and the adapters |
| `POST /v1/completions` | Yes | Text completion |
| `POST /v1/chat/completions` | Yes | Chat |

Every other path answers "not found". Per-token probabilities are refused.

```bash
curl -k -H "Authorization: Bearer <your key>" -H "Content-Type: application/json" \
  -d '{"model":"demo","prompt":"Hello","max_tokens":40}' https://<endpoint>/v1/completions
```

- The model name is `demo` for the base model, or an adapter's name.
- `-k` is needed because the certificate is made fresh at each boot and is self-signed. Its fingerprint is written to the customer's log (`SMH GATE https ... cert_sha256=...`); a careful app pins that fingerprint.
- The request and response format is the OpenAI-compatible one that vLLM provides.

## 7. Fine-tuning

### 7.1 With one command

**Customer 3**, or:

```bash
bash smh.sh finetune <file.jsonl> [steps]
# example
bash smh.sh finetune image/m4/sample/data.jsonl 150
```

It uploads the file and a job form to the customer's bucket, waits, and
prints the result and the adapter's name (for example `ft-1124044540`). Ask
the endpoint for that name as the `model`.

### 7.2 The data file

One JSON object per line, with two fields:

```json
{"prompt": "Question: Who is Zebby?\nAnswer:", "completion": " Zebby is the mascot of zeb."}
```

### 7.3 Limits set by the builder

| Limit | Value |
| --- | --- |
| Method | LoRA only |
| Rank | up to 16 (the command uses 8) |
| Steps | up to 2,000 (the command uses 150 unless you say otherwise) |
| Learning rate | between 0.000001 and 0.001 |
| Data file size | up to 50 MB |
| Jobs per day | 5 |
| Adapters served at once | 4 |

The customer sends data and a form, never code. A form with an unknown
field or a value outside the limits is refused with a reason. Only three
files leave the machine: the adapter, its settings and a short metrics file,
all stored under the customer's own key.

### 7.4 By hand

Put the data in `s3://<bucket>/data/`, then a form like
`image/m4/sample/job-good.json` in `s3://<bucket>/jobs/<name>.json`. The
result appears in `s3://<bucket>/results/<name>/status.json`.

## 8. Logs, usage and control

| What | Where | Who can read it |
| --- | --- | --- |
| The machine's log | Log group `/<NAME>/sealed-machine` | The customer |
| Usage records (counts only, every 2 minutes, sealed) | `s3://<bucket>/usage/` | The customer, and the builder (this folder only) |
| Adapters and job results | `s3://<bucket>/adapters/`, `results/` | The customer |

```bash
bash smh.sh usage      # the newest usage records of the one-click customer
```

Builder controls:

| Action | Menu | Command | Effect |
| --- | --- | --- | --- |
| Cut off one customer | Builder 4 | `bash smh.sh offboard` | That machine stops within about 4 minutes |
| Cut off everyone | Builder 6 | `bash smh.sh cutoff` | Every sealed machine stops answering within about 2 minutes and powers off |
| Restore | Builder 7 | `bash smh.sh restore` | Keys work again; machines must be started again |

Customer controls:

```bash
bash smh.sh restart    # Customer 6: stop and start the machine; the endpoint address stays the same
bash smh.sh remove     # Customer 8: delete the stack and everything in its bucket
```

## 9. The demo environment (the first customer)

The "Demo and tests" menu and the ten-point demo use the original
environment (`smh-poc-m1-*`), not the customer's stack. Remove the
customer's stack first (`bash smh.sh remove`).

| Menu | Command | What it does |
| --- | --- | --- |
| Demo 1 | `bash smh.sh demo prepare` | Starts the sealed machine, a small app and the build machine (about 19 minutes) |
| Demo 2 | `bash smh.sh demo ask "your question"` | Asks the model through the app |
| Demo 3 | `bash smh.sh demo 6` | Fine-tunes on sample data and compares the answers |
| Demo 4 | `bash smh.sh demo 7` | Shows the logs and the usage records |
| Demo 5 | (menu only) | Walks through the ten demo points, with the line to say for each |
| Demo 8 | `bash smh.sh off` | Deletes the test machines, stops the build machine |

The order and wording for a live demo are in `docs/demo-runbook.md`.
**Not run on release 0.10.0:** the demo was last run in full on 0.8.1.

## 10. Every menu entry and command

**Builder menu** (`bash smh.sh builder`)

| Entry | Command | Costs money | Notes |
| --- | --- | --- | --- |
| 1 Ship a model | `ship [model] [version]` | Yes, about 1.50 USD | Sections 3.2 to 4, in one go. One question at the start. |
| 2 Approve | `approve <version>` | A few cents | You type `yes`. Section 3.3 |
| 3 Give access | `onboard [role ARN]` | No | Section 4 |
| 4 Take access away | `offboard` | No | Stops that customer's machine |
| 5 Usage counts | `usage` | No | Section 8 |
| 6 Stop every customer | `cutoff` | No | Stops every sealed machine |
| 7 Allow them again | `restore` | No | |
| 8 Check the protections | `suite static` | No | Section 3.4 |

**Customer menu** (`bash smh.sh customer`)

| Entry | Command | Costs money | Notes |
| --- | --- | --- | --- |
| 1 Set everything up | `up` | Yes, about 1.10 USD an hour | Sections 5.2 to 5.4, in one go |
| 2 Check my endpoint | `test [adapter]` | A few cents | Section 5.4 |
| 3 Fine-tune | `finetune <file> [steps]` | No | Section 7 |
| 4 New API key | `api-key` | No | Section 5.3 |
| 5 Model in my bucket | `copy-model` | A few cents | Section 5.5 |
| 6 Restart | `restart` | No | Section 8 |
| 7 Launch Stack link | `link` | No | Section 5.2 |
| 8 Remove everything | `remove` | No | Deletes the stack and the bucket's contents |

**Demo and tests menu** (`bash smh.sh more`)

| Entry | Command | Costs money | Notes |
| --- | --- | --- | --- |
| 1 Start the demo environment | `demo prepare` | Yes, about 1.10 USD an hour | Section 9 |
| 2 Ask a question | `demo ask "text" [model]` | No | |
| 3 Fine-tune on sample data | `demo 6` | No | |
| 4 Logs and usage | `demo 7` | No | |
| 5 Guided walkthrough | (menu only) | Uses what entry 1 started | Ends with the cut-off |
| 6 Full attack suite | `suite all` | Yes, about 5 USD | Section 3.4 |
| 7 What is running | `status` | No | |
| 8 Everything off | `off` | No | |

Single steps that have no menu entry: `release <model> <version> <folder>`
(section 3.2), `install` (section 5.2), `help`.

Settings (put them before the command):

| Setting | Default | Meaning |
| --- | --- | --- |
| `DRY=1` | off | Menu only: print what would run, run nothing |
| `APPROVE=yes` | off | `ship` only: no question; approve when every check passes |
| `SMH_RAW=1` | off | Show the agent's full working notes on the screen |
| `SMH_AGENT_MODEL` | none (set it in `smh.env`) | The Bedrock inference profile the watcher thinks with (section 0) |
| `SMH_NO_WATCHER=1` | off | Run without the watcher: a failed step just stops |
| `NAME` | `smh-poc-oc` | Name prefix of the customer's stack |
| `TYPE` | from the release | Force one GPU machine type for `install` and `up` |
| `BUILD` | the newest release folder | Use another release's build folder |
| `SOURCE` | empty | `install` only: load the model from `s3://<bucket>/model/<folder>` |

## 11. Daily routine and cost

- A sealed GPU machine costs about 1.10 USD an hour. The load balancer and endpoints of the one-click stack cost about 1 USD a day even with the machine stopped.
- At the end of every day: `bash smh.sh remove` (if the customer's stack is up), then `bash smh.sh off`.
- The cost guard stops our machines every night at 21:00 IST and at 450 USD. The hard limit is 500 USD.
- For a run that must go through the night, move the nightly stop and move it back afterwards:

```bash
NIGHTLY='cron(30 3 * * ? *)' bash infra/cost-guard/deploy.sh     # 09:00 IST
NIGHTLY='cron(30 15 * * ? *)' bash infra/cost-guard/deploy.sh    # back to 21:00 IST
```

## 12. When something goes wrong

The watcher (section 0) handles most of these for you and says so on the
screen. The table is for when it is switched off, or to understand its advice.

| You see | Cause | What to do |
| --- | --- | --- |
| `STOP: wrong account` | The laptop is signed in to another account or profile | Sign in to profile `poc-ino` |
| Release says "The agent stopped early" | No free GPU machine for the encryption step | Nothing: it asks again every 10 minutes, six times. If it still fails, run the same command later; it continues where it stopped |
| `install`: "The stack could not be created" | No GPU capacity for any of the three types, or the account's network limit | Read the reason printed above it; try again later |
| `install`: "the machine did not start the model" and `key release failed` | The customer is not onboarded, or the keys are tied to another release | `bash smh.sh onboard`, then `bash smh.sh restart` |
| The endpoint answers 401 to everything | No API key yet, or a key of the wrong shape | `bash smh.sh api-key`; wait 30 seconds |
| The log says `SMH GATE keys REFUSED` | The gate could not load the key file | Check the file's lines against the rule in section 5.3 |
| The machine powered off 90 seconds after boot | No model came up (wrong files, refused key). It does not leave a GPU idle. | Read the log group for the reason |
| `suite all` fails to create its logs stack | The one-click customer is still installed | `bash smh.sh remove` first |
| A fine-tune job ends with `refused` | The form or the data broke a limit | The reason is in `status.json`; see section 7.3 |
| A long run stopped half way | The laptop slept or lost its connection | Check `bash smh.sh status`, run `bash smh.sh off`, start again on mains power |

## 13. What this guide cannot promise

- **One AWS account.** Builder and customers are separate sets of resources in it. A real second account has not been tried.
- **One model.** Only `Qwen/Qwen2.5-1.5B-Instruct` has been released and served.
- **Test Secure Boot keys**, a self-signed endpoint certificate, and a build that is not reproducible yet.
- The scripts are written for this account: the account number, profile and name prefix are at the top of each one.

More detail: `README.md` for the overview, `docs/results-summary.md` and
`docs/m10-plan.md` for results, `docs/process-report.md` for the full history.

#!/usr/bin/env bash
# Sealed model hosting: the one script for everything.
#
# The two commands most people need:
#   bash smh.sh ship <folder>   BUILDER, your OWN model: a folder on disk or a network share, or s3://..., with safetensors weights
#   bash smh.sh ship [model]    BUILDER: one question at the start, then it prepares the release, approves it if every
#                               check passes, ties the keys and gives the customer access (APPROVE=yes: no question at all)
#   bash smh.sh up              CUSTOMER: network, sealed machine, API key and a test call, in one go
#
# Or let it guide you:
#   bash smh.sh                 asks who you are, then shows a short menu with the suggested next step
#   bash smh.sh builder | customer | more     open one of those menus directly
#   DRY=1 bash smh.sh           show-only mode: prints what each entry would run
#   bash smh.sh <command>       any single action, straight from the command line (list below)
#
# The builder:
#   release <model> <version> <folder>   the agent prepares a release and stops for approval
#   approve <version>                    YOU type yes; then the agent ties both keys to the image
#   onboard [role ARN] | offboard        let one customer's machine role ask for the keys, or take that away
#   usage                                the one-click customer's usage records, as the builder may read them
#   cutoff | restore                     disable or enable both keys (cutoff stops every sealed machine)
#   suite static|all                     the attack suite: read-only checks (free), or everything (about 4.5 hours, 5 USD)
# The one-click customer:
#   link                                 print the Launch Stack link
#   install                              the same stack from the command line; ends with the endpoint address
#   api-key                              make an API key and put it where the machine reads it
#   finetune <file.jsonl> [steps]        fine-tune with this file; prints the adapter's name
#   copy-model                           copy the encrypted release into the customer's bucket, restart from it
#   test [adapter]                       a stand-in app inside the network calls the endpoint
#   restart | remove
# House:
#   demo <step>                          the ten-point demo (prepare, 1 to 10, ask "text", finish)
#   status                               what is running and what has been spent
#   off                                  delete the test machines, stop the builder, list what is left
#
# Settings: NAME (default smh-poc-oc), BUILD (default: the newest release folder), TYPE (GPU type).
# Our account is at its limit of five networks, so here the one-click stack is built inside our existing
# customer network. Those settings live in smh.env (read below if it exists); delete that file to let the
# stack make its own network.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f smh.env ]; then . ./smh.env; fi
PROFILE="poc-ino"; ACCOUNT="111122223333"; REGION="us-east-1"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }
PY="sealed-model-poc/.venv/bin/python"; ART="smh-poc-artifacts-$ACCOUNT"
NAME="${NAME:-smh-poc-oc}"; STACK="$NAME"; CUST="$NAME-customer-$ACCOUNT"
ROLE_ARN="arn:aws:iam::$ACCOUNT:role/zl/$NAME-sealed-role"
TAGS="${SMH_TAGS:-business-unit=ai env=innovation category=rnd owner=owner@example.com app=Secure-model-hosting expire-date=recurring}"   # SMH_TAGS: your organization's own required tags
KEYFILE="$HOME/.smh-poc/$NAME-api-key"      # the customer's API key, on this laptop only; never printed
BUILD="${BUILD:-$(ls -d evidence/*/build-* 2>/dev/null | while read -r d; do if [ -f "$d/customer-install/install.json" ]; then echo "$d"; fi; done | sort -t- -k2 -V | tail -1)}"
inst() { $PY -c 'import json,sys; print(json.load(open(sys.argv[1]+"/customer-install/install.json"))[sys.argv[2]])' "$BUILD" "$1"; }
out() { $A cloudformation describe-stacks --stack-name "${2:-$STACK}" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
params() {   # the six values the builder hands to the customer
  echo "Ami=$(inst image) ModelKeyArn=$(inst model_key) LeaseKeyArn=$(inst lease_key) BuilderBucket=$ART" \
       "BuilderRoleArn=arn:aws:iam::$ACCOUNT:role/zl/smh-poc-builder-role InstanceType=${TYPE:-$(inst instance_type)} Name=$NAME"
}
wait_ready() {   # the machine says MODEL READY on its console, which is also the customer's log
  local i="$1" n; say "Waiting for the model (about 12 minutes)"
  for n in $(seq 1 100); do
    $A ec2 get-console-output --instance-id "$i" --latest --query Output --output text 2>/dev/null > /tmp/oneclick-console.txt || true
    grep -q "SMH DONE loader_exit=0" /tmp/oneclick-console.txt && { echo "model ready"; return 0; }
    grep -q "SMH DONE loader_exit=[1-9]\|key release failed" /tmp/oneclick-console.txt && { grep "SMH" /tmp/oneclick-console.txt | tail -5; echo "the machine did not start the model"; grep -q "key release failed" /tmp/oneclick-console.txt && echo "It was refused the key. The builder must give this customer access: bash smh.sh onboard   (then: bash smh.sh restart)"; return 1; }
    sleep 20
  done
  echo "timed out"; return 1
}


# ------------------------------------------------------------------ the menu
B=$'\033[1m'; G=$'\033[32m'; D=$'\033[2m'; N=$'\033[0m'
run() { if [ -n "${DRY:-}" ]; then echo "${D}would run:${N} $*"; else "$@" || echo "${D}(that step ended with an error; see above)${N}"; fi; }
ok() { local a; read -r -p "$1 [y/N] " a; [ "$a" = y ] || [ "$a" = Y ]; }
keys() { local k; for k in "$@"; do $A kms describe-key --key-id "alias/smh-poc-$k-key" --query KeyMetadata.KeyId --output text; done; }
overview() {
  echo; echo "${B}Sealed model hosting${N}"
  echo "  current release : $(inst version) ($(inst image))"
  echo "  keys            : model $($A kms describe-key --key-id alias/smh-poc-model-key --query KeyMetadata.KeyState --output text), lease $($A kms describe-key --key-id alias/smh-poc-lease-key --query KeyMetadata.KeyState --output text)"
  local m; m="$($A ec2 describe-instances --filters Name=tag:app,Values=Secure-model-hosting Name=instance-state-name,Values=pending,running --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,InstanceType]' --output text | awk '{printf "%s (%s)  ", $1, $2}')"
  echo "  running now     : ${m:-nothing}"
  echo "  one-click stack : $($A cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].Outputs[?OutputKey==`Endpoint`]|[0].OutputValue' --output text 2>/dev/null || echo "not installed")"
  echo "  spend estimate  : $($A ssm get-parameter --name /smh-poc/cost-guard/state --query Parameter.Value --output text | $PY -c 'import json,sys; print("$%.2f of $500" % json.load(sys.stdin)["usd"])')"
}
# The guided walkthrough: the runbook's order, one point at a time, with the line to say.
WALK=(
"bad|While we talk, a tampered copy of the image is booting. We will see what happens to it."
"1|This is what the customer's account holds: noise. And this is our own admin being refused the key."
"2|There is no way in. Not for the customer, and not for us."
"5|The customer's app, on the customer's private network."
"6|The customer teaches the model one made-up fact. Data and a form, never code."
"7|The logs are the customer's. The builder gets counts, and the customer can read exactly what the builder gets."
"4|Loading. And here we are honest: memory is not flat."
"3|The tampered copy: no key, and it switched itself off."
"9|This release was prepared by an agent from one sentence. Watch it refuse to tie the keys to an image on my word."
"10|Thirty-three tests, each tied to a property, run on every image."
"8|Last: the builder withdraws access. This ends the sealed machine."
)
walkthrough() {
  echo "The walkthrough needs the demo environment running (entry 1 of this menu). It ends with the cut-off."
  ok "Start the walkthrough?" || return 0
  local item step line a q
  for item in "${WALK[@]}"; do
    step="${item%%|*}"; line="${item#*|}"
    echo; echo "${G}Say:${N} $line"
    read -r -p "${D}Enter to run point $step, s to skip, q to stop${N} " a
    case "$a" in q) return 0;; s) continue;; esac
    if [ "$step" = 5 ]; then read -r -p "A question from the room (Enter for the default): " q; run bash demo.sh 5 ${q:+"$q"}; else run bash demo.sh "$step"; fi
  done
  echo; echo "Done. Choose 'Everything off' when the room has no more questions."
}
# What is the state, and so what is the sensible next step
T_OF() { echo "evidence/releases/agent-$1.txt"; }
waiting_release() {   # a release the agent prepared that nobody approved yet
  local t v; t="$(ls -t evidence/releases/agent-*.txt 2>/dev/null | head -1)"; [ -n "$t" ] || return 0
  if tail -3 "$t" | grep -q "FINAL STATUS: NEEDS_APPROVAL"; then v="${t##*agent-}"; echo "${v%.txt}"; fi
}
onboarded() { $A cloudformation describe-stacks --stack-name smh-poc-m1-key --query "Stacks[0].Parameters[?ParameterKey=='CustomerRoleArn'].ParameterValue" --output text 2>/dev/null | grep -q "role/"; }
installed() {   # the customer's stack exists and is not half-made or half-deleted
  case "$($A cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)" in
    CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) return 0;; *) return 1;; esac
}
has_key() { $A s3api head-object --bucket "$CUST" --key gate/api-keys.txt >/dev/null 2>&1; }
next_builder() {
  local w; w="$(waiting_release)"
  if [ -n "$w" ]; then echo "Release $w is waiting for your approval: choose 2."
  elif ! onboarded; then echo "No customer has access yet: choose 3."
  else echo "Nothing is waiting. Release $(inst version) is live and the customer has access."; fi
}
next_customer() {
  if ! installed; then echo "Nothing is set up yet: choose 1."
  elif ! has_key; then echo "The machine is up but there is no API key: choose 4."
  else echo "You are set up. Endpoint: $(out Endpoint)"; fi
}
again() { read -r -p "${D}Enter to go on${N} " _ || exit 0; }
# The agent's full working notes go to a file; the screen gets plain progress lines (SMH_RAW=1 shows everything).
shown() { if [ -n "${SMH_RAW:-}" ]; then cat; else python3 deployer/progress.py; fi; }

# The watcher. Every step of ship and up runs through here. When a step fails, an AI agent (agent/fixer.py) looks
# into it with read-only tools, then either picks one fix from a short fixed list, which is carried out below and
# the step is run again, or tells the person exactly what to do and waits. SMH_NO_WATCHER=1 switches it off.
guard() {   # guard <builder|customer> <step name> <command...>
  local role="$1" step="$2"; shift 2
  local n log out act val a st
  for n in 1 2 3 4 5; do
    log="$(mktemp)"
    if "$@" 2>&1 | tee "$log"; then rm -f "$log"; return 0; fi
    if [ -n "${SMH_NO_WATCHER:-}" ] || [ "$n" = 5 ]; then rm -f "$log"; return 1; fi
    say "Watcher: the step \"$step\" failed (try $n). Looking into it."
    out="$($PY agent/fixer.py --role "$role" --step "$step" --log "$log" --name "$NAME" --version "$(inst version)" --model "${MODEL:-}" --attempt "$n" | tee /dev/stderr)" || true
    rm -f "$log"
    act="$(printf '%s\n' "$out" | sed -n 's/^ACTION //p' | tail -1)"; val="${act#* }"; [ "$val" = "$act" ] && val=""; act="${act%% *}"
    case "$act" in
      retry) ;;
      wait_retry) for a in $(seq "${val:-10}" -1 1); do echo "  $(date +%H:%M)  waiting, $a minute(s) to go | nothing to type"; sleep 60; done ;;
      other_machine_type) if [ -n "$val" ]; then export TYPE="$val"; echo "  using machine type $val"; fi ;;
      clean_failed_stack)
        st="$($A cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
        case "$st" in ROLLBACK_COMPLETE|ROLLBACK_FAILED|CREATE_FAILED|DELETE_FAILED) bash "$0" remove || true ;; *) echo "  the stack is in state ${st:-none}; not deleting it" ;; esac ;;
      restart_machine) bash "$0" restart || true ;;
      new_api_key) bash "$0" api-key && sleep 45 ;;
      onboard) bash "$0" onboard ;;
      *) if [ -t 0 ]; then read -r -p "  Press Enter when that is done and I will continue, or type q to stop: " a; if [ "$a" = q ]; then return 1; fi
         else echo "  Nobody is at the keyboard, so this stops here. Run the same command again when it is done."; return 1; fi ;;
    esac
    say "Watcher: running the step \"$step\" again"
  done
  return 1
}

builder_menu() {
  local c v a model
  while :; do
    overview; echo; echo " ${G}Next:${N} $(next_builder)"
    cat <<MENU

 ${B}Model builder${N}
   1  Ship a model: one question now, then it runs alone to the end (about 1 hour, about 1.50 USD)
   2  Approve a release that is waiting (only if you chose to decide at the end)
   3  Give the customer access           4  Take the customer's access away
   5  Read the customer's usage counts
   6  Stop every customer                7  Allow them again
   8  Check that the protections hold (read-only, free, 1 minute)
   b  Back                               q  Quit
MENU
    read -r -p "> " c || exit 0
    case "$c" in
      1) read -r -p "Model on Hugging Face [Qwen/Qwen2.5-1.5B-Instruct]: " model
         if ok "Start a GPU helper and the build machine now?"; then run caffeinate -ims bash "$0" ship "${model:-Qwen/Qwen2.5-1.5B-Instruct}"; fi;;
      2) v="$(waiting_release)"; read -r -p "Version to approve [${v:-none waiting}]: " a; v="${a:-$v}"
         if [ -n "$v" ]; then echo "${B}This is the human decision.${N} Read the summary, then type yes yourself."; run caffeinate -ims bash "$0" approve "$v"; fi;;
      3) run bash "$0" onboard;;
      4) if ok "Take the customer's access away? Their machine stops within about 4 minutes."; then run bash "$0" offboard; fi;;
      5) run bash "$0" usage;;
      6) if ok "Disable both keys? Every sealed machine stops answering within about 2 minutes and powers off."; then run bash "$0" cutoff; fi;;
      7) run bash "$0" restore;;
      8) run bash "$0" suite static;;
      b|B) return 0;; q|Q) exit 0;;
      *) echo "Pick a number from the menu.";;
    esac
    again
  done
}

customer_menu() {
  local c f a
  while :; do
    overview; echo; echo " ${G}Next:${N} $(next_customer)"
    cat <<MENU

 ${B}Customer${N}
   1  Set everything up: network, sealed machine, API key, test call (about 25 minutes, about 1.10 USD an hour)
   2  Check my endpoint (a test call from inside my network)
   3  Teach the model from my own file (fine-tune)
   4  Make a new API key
   5  Keep the model in my own bucket (about 25 minutes)
   6  Restart my machine                 7  Show the Launch Stack link
   8  Remove everything of mine
   b  Back                               q  Quit
MENU
    read -r -p "> " c || exit 0
    case "$c" in
      1) if ok "Create the stack with one GPU machine?"; then run caffeinate -ims bash "$0" up; fi;;
      2) read -r -p "Adapter name to ask as well (Enter for none): " a; run bash "$0" test ${a:+"$a"};;
      3) read -r -p "Training file (.jsonl) [image/m4/sample/data.jsonl]: " f; run bash "$0" finetune "${f:-image/m4/sample/data.jsonl}";;
      4) run bash "$0" api-key;;
      5) if ok "Copy about 15 GB of encrypted files into your bucket and restart the machine?"; then run caffeinate -ims bash "$0" copy-model; fi;;
      6) if ok "Stop and start the machine? It answers again in about 12 minutes, at the same address."; then run caffeinate -ims bash "$0" restart; fi;;
      7) run bash "$0" link;;
      8) if ok "Delete the stack and everything in its bucket?"; then run bash "$0" remove; fi;;
      b|B) return 0;; q|Q) exit 0;;
      *) echo "Pick a number from the menu.";;
    esac
    again
  done
}

more_menu() {
  local c q a
  while :; do
    overview
    cat <<MENU

 ${B}Demo and tests${N} (these use the first, demo environment; remove the customer's stack first)
   1  Start the demo environment (about 19 minutes, about 1.10 USD an hour)
   2  Ask the model a question           3  Fine-tune on sample data and compare
   4  Show the logs and usage records    5  Guided walkthrough of the ten demo points
   6  Run the full attack suite (about 4.5 hours, about 5 USD)
   7  What is running                    8  Everything off
   b  Back                               q  Quit
MENU
    read -r -p "> " c || exit 0
    case "$c" in
      1) if ok "Start a GPU machine, a small client and the build machine?"; then run caffeinate -ims bash demo.sh prepare; fi;;
      2) read -r -p "Question: " q; if [ -n "$q" ]; then run bash demo.sh ask "$q"; fi;;
      3) run bash demo.sh 6;;
      4) run bash demo.sh 7;;
      5) walkthrough;;
      6) if ok "Start the full suite? Keep the laptop on mains power."; then run bash "$0" suite all; fi;;
      7) run bash "$0" status;;
      8) if ok "Delete the test machines and stop the build machine?"; then run bash "$0" off; fi;;
      b|B) return 0;; q|Q) exit 0;;
      *) echo "Pick a number from the menu.";;
    esac
    again
  done
}

menu() {
  local c
  while :; do
    cat <<MENU

${B}Sealed model hosting${N}

 Who are you today?
   1  The model builder   (I own the model and release it)
   2  The customer        (I run the model in my own AWS account)
   3  Demo and tests
   q  Quit
MENU
    read -r -p "> " c || exit 0
    case "$c" in 1) builder_menu;; 2) customer_menu;; 3) more_menu;; q|Q) exit 0;; *) echo "Type 1, 2, 3 or q.";; esac
  done
}

case "${1:-menu}" in
menu) menu ;;
builder) builder_menu ;;
customer) customer_menu ;;
more) more_menu ;;
ship)   # the builder's whole job in one command
  MODEL="${2:-Qwen/Qwen2.5-1.5B-Instruct}"; V="${3:-$(inst version | awk -F. '{print $1"."$2"."$3+1}')}"; F="m$(echo "$V" | tr -d .)"
  if [ -d "$MODEL" ] || [ "${MODEL#s3://}" != "$MODEL" ]; then
    # Your OWN model: a folder on this disk or a network share, or an s3:// location you can read. It must be a
    # Hugging Face style folder (config.json, tokenizer files, .safetensors weights). It is staged in the builder's
    # bucket, encrypted by the release, and the staged plain copy is deleted as soon as the encryption is done.
    SRC_MODEL="${MODEL%/}"; OWN_NAME="$(basename "$SRC_MODEL" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9._-\n' '-' | cut -c1-60)"; STAGE="s3://$ART/deploy/models-in/$OWN_NAME"
    say "Staging your own model \"$OWN_NAME\" from $SRC_MODEL"
    $A s3 sync --only-show-errors --delete --exclude "*" --include "*.safetensors" --include "*.json" --include "*.txt" --include "*.model" --include "*.jinja" "$SRC_MODEL/" "$STAGE/"
    $A s3api list-objects-v2 --bucket "$ART" --prefix "deploy/models-in/$OWN_NAME/" --query 'Contents[].[Key,Size]' --output json | $PY -c 'import json,sys
rows=json.load(sys.stdin) or []; sib=[{"rfilename": k.split("/")[-1], "size": s} for k, s in rows if not k.endswith("model-info.json")]
names={x["rfilename"] for x in sib}
if "config.json" not in names: sys.exit("This is not a model folder I can use: there is no config.json in it.")
if not any(n.endswith(".safetensors") for n in names): sys.exit("This model has no .safetensors weight files. Save it with save_pretrained(..., safe_serialization=True) first.")
json.dump({"id": sys.argv[1], "siblings": sib, "gated": False, "private": False, "source": "the builder\x27s own weights, staged from a folder; not on Hugging Face"}, open(sys.argv[2], "w"))
print("  staged %d files, %.2f GiB of weights" % (len(sib), sum(x["size"] for x in sib if x["rfilename"].endswith(".safetensors")) / 2**30))' "local/$OWN_NAME" /tmp/smh-model-info.json
    $A s3 cp --only-show-errors /tmp/smh-model-info.json "$STAGE/model-info.json"
    MODEL="local/$OWN_NAME"
  fi
  say "Shipping $MODEL as release $V"
  # The approval is asked ONCE, here at the start, not an hour into the run. APPROVE=yes skips even that.
  # The release is then approved by itself, but only if every one of its checks passes; otherwise it stops and waits.
  if [ "${APPROVE:-}" != yes ]; then
    read -r -p "Approve this release by itself when all its checks pass? Type yes (anything else: I will ask at the end): " PRE
    if [ "$(printf '%s' "$PRE" | tr 'A-Z' 'a-z' | tr -d '[:space:]')" = yes ]; then APPROVE=yes; fi
  fi
  export APPROVE
  if [ "${APPROVE:-}" = yes ]; then echo "  Approved in advance. Nothing more is needed from you: this runs to the end by itself (about 1 hour)."
  else echo "  You will be asked once, in about 1 hour: a sound, a notification and a banner will say YOUR TURN."; fi
  echo "Step 1 of 3: preparing the release"
  export MODEL SMH_RELEASING="$V"      # the watcher reads these to look at the right model and release
  guard builder "prepare the release" bash "$0" release "$MODEL" "$V" "$F" || true
  if ! tail -3 "$(T_OF "$V")" | grep -q "FINAL STATUS: NEEDS_APPROVAL"; then if [ -n "${OWN_NAME:-}" ] && grep -q '"step\\*":\\*"lock\\*",\\*"state\\*":\\*"done' "$(T_OF "$V")"; then $A s3 rm --only-show-errors --recursive --exclude model-info.json "s3://$ART/deploy/models-in/$OWN_NAME/" || true; fi; echo "The release did not reach the approval step. Read $(T_OF "$V"), then run the same command again; it continues where it stopped."; exit 1; fi
  # An own model that was staged again for a second try (its encryption was already done) must not stay in plain form.
  if [ -n "${OWN_NAME:-}" ]; then $A s3 rm --only-show-errors --recursive --exclude model-info.json "s3://$ART/deploy/models-in/$OWN_NAME/" || true; fi
  say "Step 2 of 3: approval"
  if [ "${APPROVE:-}" = yes ]; then
    # Approved in advance, on one condition: every check of this exact build says PASS.
    if $A s3 cp --only-show-errors "s3://$ART/deploy/$V/approval-request.json" - | $PY -c 'import json,sys
r=json.load(sys.stdin); c=r.get("checks") or {}
bad=[k for k,v in c.items() if v!="PASS"]
if bad or len(c)<6 or r.get("image_check")!="OK": print("  not every check passed:", bad or c, r.get("image_check")); sys.exit(1)
print("  all %d checks PASS, image check OK: approving as agreed at the start" % len(c))'; then
      guard builder "approve and tie the keys to the image" bash -c "printf 'yes\n' | bash '$0' approve '$V'"
    else
      echo "  This release is NOT approved by itself. Read the summary and decide:"; bash "$0" approve "$V"
    fi
  else
    bash "$0" approve "$V"
  fi
  say "Step 3 of 3: customer access"
  guard builder "give the customer access" bash "$0" onboard
  say "Release $V is live. The customer can now run: bash smh.sh up" ;;
up)     # the customer's whole job in one command
  say "Setting up the customer's side: network, sealed machine, API key, test call (about 25 minutes)"
  if ! onboarded; then echo "Note: the builder has not given this customer access yet (bash smh.sh onboard). The machine will be refused the key until then."; fi
  guard customer "create the stack and start the sealed machine" bash "$0" install || { echo "The machine is not serving, so the API key and the test call were skipped. Run the same command again when the cause above is fixed."; exit 1; }
  if ! has_key || [ ! -s "$KEYFILE" ]; then guard customer "make an API key" bash "$0" api-key; sleep 45; fi
  guard customer "call the endpoint" bash "$0" test
  say "Ready"
  echo "Endpoint : $(out Endpoint)   (reachable from inside the customer's network only)"
  echo "API key  : in $KEYFILE"
  echo "Call it  : curl -k -H \"Authorization: Bearer <key>\" -H \"Content-Type: application/json\" -d '{\"model\":\"demo\",\"prompt\":\"Hello\",\"max_tokens\":40}' $(out Endpoint)/v1/completions"
  echo "Next     : bash smh.sh finetune <file.jsonl>      Remove: bash smh.sh remove" ;;
release)
  mkdir -p evidence/releases; T="$(T_OF "$3")"; HINT="The lock step takes about 25 minutes; it is not stuck before 45."
  # The agent's full working notes go to the file $T. The screen shows plain progress lines only (SMH_RAW=1 shows everything).
  echo "  Full notes: $T"
  OUT="$T" bash deployer/run.sh "Deploy $2 as version $3 in folder $4. $HINT" 2>&1 | shown || true
  # No blind retry here: when the agent stops early, the watcher (guard) finds out why. It waits and retries for a
  # passing cause such as no free GPU machine, and goes to a person for a real one such as a model that cannot be used.
  if ! tail -3 "$T" | grep -q "FINAL STATUS: \(NEEDS_APPROVAL\|DONE\)"; then
    echo "The release did not reach the approval step. The end of the agent's notes:"; grep -v "tool call\|\[stop" "$T" | grep -v "^$" | tail -14 | cut -c1-1500; exit 1
  fi
  if [ "${APPROVE:-}" != yes ]; then echo "Ready for your approval: bash smh.sh approve $3"; fi ;;
approve)
  bash deployer/approve.sh "$2"       # asks for a typed yes
  S="$(grep -o 'session [0-9a-f-]\{36\}' "evidence/releases/agent-$2.txt" | tail -1 | cut -d' ' -f2)"
  echo "  Tying both keys to the image (about 5 minutes)"
  SESSION="$S" OUT="evidence/releases/agent-$2.txt" bash deployer/run.sh "Continue." 2>&1 | shown || true
  D="evidence/releases/build-$2"; mkdir -p "$D"
  $A s3 sync --only-show-errors --exclude "*.packages" "s3://$ART/build/out-$2/" "$D/"
  $A s3 cp --only-show-errors "s3://$ART/deploy/$2/logs/build.log" "$D/pipeline.log"
  echo "build folder: $D" ;;
onboard|offboard)
  R="${2:-$ROLE_ARN}"; if [ "$1" = offboard ]; then R=""; fi
  say "Key policies: customer role = ${R:-nobody}"
  CUSTOMER_ROLE="$R" bash infra/m1/deploy.sh key > /dev/null; CUSTOMER_ROLE="$R" bash infra/m1/deploy.sh lease > /dev/null
  echo "done. The measurements are still required; the role alone gets nothing." ;;
link)
  $A s3 cp --only-show-errors infra/oneclick/customer.yaml "s3://$ART/oneclick/customer.yaml"
  Q="stackName=$NAME&templateURL=https://$ART.s3.$REGION.amazonaws.com/oneclick/customer.yaml"
  for p in $(params); do Q="$Q&param_${p%%=*}=$($PY -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "${p#*=}")"; done
  echo "https://$REGION.console.aws.amazon.com/cloudformation/home?region=$REGION#/stacks/quickcreate?$Q"
  echo "(In THIS account also add the six tags on the wizard's tag page; the organization refuses untagged resources.)" ;;
install)
  say "Creating the customer's side in one stack: $STACK (release $(inst version), $(inst image))"
  # GPU capacity comes and goes: try the release's machine type, then two others. A failed first create
  # leaves an empty stack that cannot be updated, so it is deleted before the next try.
  DONE=""
  # A machine type that already works for this stack comes first, so a second run does not swap the machine.
  NOW_TYPE="$($A cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Parameters[?ParameterKey=='InstanceType'].ParameterValue" --output text 2>/dev/null || true)"
  for t in ${TYPE:-$NOW_TYPE $(inst instance_type) g6.xlarge g5.2xlarge}; do
    if [ "$($A cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null)" = ROLLBACK_COMPLETE ]; then
      $A cloudformation delete-stack --stack-name "$STACK"; $A cloudformation wait stack-delete-complete --stack-name "$STACK"
    fi
    echo "machine type: $t"
    # shellcheck disable=SC2046,SC2086
    if TYPE="$t" && $A cloudformation deploy --stack-name "$STACK" --template-file infra/oneclick/customer.yaml --capabilities CAPABILITY_NAMED_IAM \
      --no-fail-on-empty-changeset --parameter-overrides $(params) ${SOURCE+ModelSource="$SOURCE"} ${OC_EXTRA:-} --tags $TAGS; then DONE=1; break; fi
    $A cloudformation describe-stack-events --stack-name "$STACK" --max-items 40 --query 'StackEvents[?contains(ResourceStatus,`FAILED`)]|[-1].ResourceStatusReason' --output text 2>/dev/null | cut -c1-200
  done
  [ -n "$DONE" ] || { echo "The stack could not be created (see the reason above). Nothing is left running; try again later."; exit 1; }
  # CloudFormation cannot tag instance profiles.
  $A iam tag-instance-profile --instance-profile-name "$NAME-sealed-profile" --tags $(for t in $TAGS; do echo "Key=${t%%=*},Value=${t#*=}"; done)
  echo "endpoint: $(out Endpoint)"; echo "API keys go in: $(out ApiKeyFile)   (bash smh.sh api-key)"
  wait_ready "$(out InstanceId)" ;;
api-key)
  mkdir -p "$(dirname "$KEYFILE")"; (umask 077; openssl rand -hex 24 > "$KEYFILE")
  $A s3 cp --only-show-errors --sse aws:kms --sse-kms-key-id "$(out KeyArn)" "$KEYFILE" "s3://$CUST/gate/api-keys.txt"
  echo "A new API key is in $KEYFILE and in $(out ApiKeyFile). The machine picks it up within 30 seconds." ;;
finetune) guard customer "fine-tune with a file" bash "$0" _finetune "${@:2}" ;;
_finetune)
  F="${2:?a .jsonl file}"; J="ft-$(date -u +%m%d%H%M%S)"; K="$(out KeyArn)"; T="$(mktemp -d)"
  $PY -c 'import json,sys; json.dump({"method": "lora", "dataset_uri": sys.argv[1], "output_uri": sys.argv[2], "rank": 8, "alpha": 16, "learning_rate": 0.0002, "max_steps": int(sys.argv[3]), "target_modules": ["q_proj", "v_proj"], "seed": 1}, open(sys.argv[4], "w"), indent=1)' \
    "s3://$CUST/data/$J.jsonl" "s3://$CUST/adapters/" "${3:-150}" "$T/form.json"
  $A s3 cp --only-show-errors --sse aws:kms --sse-kms-key-id "$K" "$F" "s3://$CUST/data/$J.jsonl"
  $A s3 cp --only-show-errors --sse aws:kms --sse-kms-key-id "$K" "$T/form.json" "s3://$CUST/jobs/$J.json"
  say "Job $J sent. Waiting for the result (a small job takes about 2 minutes)"
  for _ in $(seq 1 90); do
    if $A s3 cp --only-show-errors "s3://$CUST/results/$J/status.json" "$T/status.json" 2>/dev/null && ! grep -q '"state": "running"' "$T/status.json"; then
      cat "$T/status.json"; echo
      if grep -q '"state": "done"' "$T/status.json"; then echo "Ask the endpoint for model \"$J\"."; rm -rf "$T"; exit 0; fi
      echo "The job did not finish as done; the reason is in the status above."; rm -rf "$T"; exit 1
    fi
    sleep 10
  done
  echo "no result after 15 minutes"; exit 1 ;;
copy-model)
  FOLDER="$(inst folder)"; K="$(out KeyArn)"
  say "Copying the encrypted release $FOLDER into s3://$CUST/model/$FOLDER/ (about 15 GB, ciphertext only)"
  $A s3 sync --only-show-errors --exact-timestamps --sse aws:kms --sse-kms-key-id "$K" "s3://$ART/$FOLDER/" "s3://$CUST/model/$FOLDER/"
  $A s3api head-object --bucket "$CUST" --key "model/$FOLDER/lease.token" >/dev/null
  SOURCE="s3://$CUST/model/$FOLDER" bash "$0" install >/dev/null 2>&1 || true     # sets the tag; a tag change does not restart
  bash "$0" restart ;;
restart)
  I="$(out InstanceId)"; say "Restarting $I"
  $A ec2 stop-instances --instance-ids "$I" >/dev/null; $A ec2 wait instance-stopped --instance-ids "$I"
  $A ec2 start-instances --instance-ids "$I" >/dev/null; $A ec2 wait instance-running --instance-ids "$I"
  sleep 60; wait_ready "$I"; echo "endpoint (unchanged): $(out Endpoint)" ;;
usage)   # run with the builder's rights in real life; here it shows what is in the folder the builder may read
  $A s3 ls "s3://$CUST/usage/" --recursive | tail -5 ;;
test)
  [ -s "$KEYFILE" ] || { echo "no API key yet: bash smh.sh api-key"; exit 1; }
  E="$(out Endpoint)"; AD="${2:-demo}"      # with no adapter named, the base model is asked
  $A cloudformation describe-stacks --stack-name "$STACK-app" >/dev/null 2>&1 && { $A cloudformation delete-stack --stack-name "$STACK-app"; $A cloudformation wait stack-delete-complete --stack-name "$STACK-app"; }
  $A cloudformation deploy --stack-name "$STACK-app" --template-file infra/oneclick/app.yaml --no-fail-on-empty-changeset --tags $TAGS \
    --parameter-overrides VpcId="$(out VpcId)" SubnetId="$(out SubnetId)" Name="$NAME" ${OC_CIDR:+Cidr="$OC_CIDR"} \
      UserData="$(sed -e "s|TARGET|${E#https://}|" -e "s|APIKEY|$(cat "$KEYFILE")|" -e "s|zebby-1|$AD|" -e "s|CUSTBUCKET|none|" -e "s|CUSTKEY|none|" infra/m1/client-test.sh)" >/dev/null
  I="$(out InstanceId "$STACK-app")"; say "Test app $I is calling $E"
  for _ in $(seq 1 40); do
    sleep 15; $A ec2 get-console-output --instance-id "$I" --latest --query Output --output text 2>/dev/null | grep "SMH CLIENT" > /tmp/oneclick-app.txt || true
    grep -q "answer\[$AD\]" /tmp/oneclick-app.txt && break
  done
  grep -v " tick \| door \| port .* closed" /tmp/oneclick-app.txt | cut -c1-200 | tail -14
  echo "doors answering 404: $(grep -c ' door .*-> 404' /tmp/oneclick-app.txt) of 13; ports open: $(grep ' port .*open' /tmp/oneclick-app.txt | awk '{print $5}' | tr '\n' ' ')"
  if ! grep -q "answer\[$AD\] 200" /tmp/oneclick-app.txt; then echo "The endpoint did not answer the test question for model \"$AD\"."; exit 1; fi ;;
status)
  overview
  I="$(out InstanceId 2>/dev/null || true)"; if [ -n "$I" ]; then $A ec2 describe-instances --instance-ids "$I" --query 'Reservations[0].Instances[0].[InstanceId,InstanceType,State.Name]' --output text; fi ;;
cutoff)  for k in $(keys lease model); do $A kms disable-key --key-id "$k"; done; echo "both keys disabled" ;;
restore) for k in $(keys model lease); do $A kms enable-key --key-id "$k"; done; echo "both keys enabled" ;;
suite)
  case "${2:?static or all}" in
    static) R="evidence/releases/suite-static-$(inst version)"
         # The suite's own report lists the live and attack tests as NOT RUN; here only the read-only ones were asked for.
         VERSION="$(inst version)" BUILD="$BUILD" RUN="$R" bash assure.sh static 2>&1 | grep -v "NOT RUN [LBR][0-9]\| not run -> " || true
         $PY -c 'import json,sys
r=[t for t in json.load(open(sys.argv[1]+"/results.json")) if t["id"].startswith("S")]
bad=[t for t in r if t["verdict"]!="PASS" and t["id"]!="S10"]
print("read-only checks: %d of %d pass (S10, the reproducible build, is a known open item)" % (sum(t["verdict"]=="PASS" for t in r), len(r)-1))
for t in bad: print("  %s %s: %s" % (t["verdict"], t["id"], t["seen"][:150]))
print("The live and attack tests need: bash smh.sh suite all")
sys.exit(1 if bad else 0)' "$R" ;;
    all) if [ ! -f "$BUILD/breaks.json" ]; then echo "note: no attack images are registered for this release, so B2 and B3 will be NOT RUN (see README)"; fi
         VERSION="$(inst version)" BUILD="$BUILD" RUN="evidence/releases/suite-$(inst version)" caffeinate -ims bash assure.sh all ;;
    *) echo "static or all"; exit 2 ;;
  esac ;;
demo) shift; bash demo.sh "$@" ;;
off)  bash demo.sh finish ;;
remove)
  for s in "$STACK-app" "$STACK"; do
    $A cloudformation describe-stacks --stack-name "$s" >/dev/null 2>&1 || continue
    if [ "$s" = "$STACK" ]; then
      # Stop the machine first: a running machine writes a usage record every two minutes (and a last one as it
      # stops), and a bucket that is not empty cannot be deleted.
      I="$(out InstanceId 2>/dev/null || true)"
      if [ -n "$I" ]; then $A ec2 stop-instances --instance-ids "$I" >/dev/null 2>&1 || true; $A ec2 wait instance-stopped --instance-ids "$I" 2>/dev/null || true; fi
      $A s3 rm --only-show-errors --recursive "s3://$CUST/" 2>/dev/null || true
    fi
    $A cloudformation delete-stack --stack-name "$s"
    if ! $A cloudformation wait stack-delete-complete --stack-name "$s" 2>/dev/null; then    # one more try, for anything written in between
      $A s3 rm --only-show-errors --recursive "s3://$CUST/" 2>/dev/null || true
      $A cloudformation delete-stack --stack-name "$s"; $A cloudformation wait stack-delete-complete --stack-name "$s"
    fi
    echo "deleted $s"
  done ;;
help|-h|--help) sed -n 2,40p "$0" ;;
*) sed -n 2,40p "$0"; exit 2 ;;
esac

#!/usr/bin/env bash
# The human's step. Shows what the deployer asks to pin, checks that the
# summary matches the build's own output file, asks for a typed "yes", and
# writes the approval record. Only a human with the poc-ino profile can
# write that record; the deployer's roles are denied.
#   bash deployer/approve.sh 0.7.0
set -euo pipefail
V="${1:?version}"; PROFILE="poc-ino"; ACCOUNT="111122223333"
A="aws --profile $PROFILE --region us-east-1 --no-cli-pager"; B="s3://smh-poc-artifacts-$ACCOUNT"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
$A s3 cp --only-show-errors "$B/deploy/$V/approval-request.json" "$T/request.json" || { echo "no approval request for $V"; exit 1; }
$A s3 cp --only-show-errors "$B/build/out-$V/summary.json" "$T/summary.json"
python3 - "$T" "$V" <<'PY'
import hashlib, json, sys
t, v = sys.argv[1], sys.argv[2]
r = json.load(open(t + "/request.json")); raw = open(t + "/summary.json", "rb").read(); s = json.loads(raw)
sha = hashlib.sha256(raw).hexdigest()
problems = []
if r.get("version") != v or s.get("version") != v: problems.append("version does not match")
if r.get("summary_sha256") != sha: problems.append("the request does not match the build's output file")
for a, b in (("pcr4", "PCR4"), ("pcr7", "PCR7"), ("pcr12", "PCR12"), ("pcr15_open", "PCR15_open"), ("pcr15_locked", "PCR15_locked")):
    if r.get(a) != s.get(b): problems.append(f"{b} in the request differs from the build")
print(f"\nRelease {v}: {r.get('model')}  (folder {r.get('folder')}, machine {r.get('gpu')})")
print(f"  image            {r.get('ami')}")
for k in ("pcr4", "pcr7", "pcr12", "pcr15_open", "pcr15_locked"):
    print(f"  {k:<16} {str(r.get(k))[:24]}...")
print(f"  packages         {r.get('packages')}   no-way-in check: {r.get('image_check')}")
print("  checks           " + "  ".join(f"{k}={v}" for k, v in (r.get("checks") or {}).items()))
print(f"  effect           {r.get('effect')}\n")
if problems:
    print("DO NOT APPROVE: " + "; ".join(problems)); sys.exit(1)
json.dump({"version": v, "summary_sha256": sha}, open(t + "/approval.json", "w"))
PY
# Anything typed before this point was typed without seeing the summary above. Throw it away,
# so that only an answer given AFTER reading counts. (Keyboard only; piped input is left alone.)
if [ -t 0 ]; then python3 -c 'import termios; termios.tcflush(0, termios.TCIFLUSH)' 2>/dev/null || true; fi
# Make the moment impossible to miss: a sound, a desktop notification, and a banner (keyboard only).
if [ -t 0 ]; then
printf '\a'
osascript -e 'display notification "The release is ready. Go to the terminal and type yes to approve it." with title "Sealed model hosting: YOUR TURN" sound name "Glass"' >/dev/null 2>&1 || true
cat <<'BANNER'

  ============================================================
   YOUR TURN.  The release above is ready and waits for you.
   1. Read the summary above. Every check should say PASS.
   2. Click in this window, type   yes   and press Enter.
      (Anything else, or just Enter, refuses the release.)
  ============================================================

BANNER
fi
read -r -p "Type yes to approve pinning both keys to this image: " ANSWER
ANSWER="$(printf '%s' "$ANSWER" | tr 'A-Z' 'a-z' | tr -d '[:space:]')"
[ "$ANSWER" = yes ] || { echo "not approved (the answer must be the word yes)"; exit 1; }
python3 - "$T" "$($A sts get-caller-identity --query Arn --output text)" <<'PY'
import json, sys, datetime
p = sys.argv[1] + "/approval.json"; a = json.load(open(p))
a.update(approved_by=sys.argv[2], approved_at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
json.dump(a, open(p, "w"), indent=1)
PY
$A s3 cp --only-show-errors "$T/approval.json" "$B/approvals/$V.json"
echo "approved $V. Ask the deployer to continue."

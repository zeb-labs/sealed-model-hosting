#!/usr/bin/env bash
# Start or continue a deployment with the agent.
#   bash deployer/run.sh "Deploy Qwen/Qwen2.5-1.5B-Instruct as version 0.7.0 in folder m7"
#   SESSION=<id from the first run> bash deployer/run.sh "Continue."
# Settings: ROUNDS (how many times to ask it to continue, default 40), PAUSE (seconds between, default 120),
# OUT (transcript file, default evidence/m7/transcript.txt), HARNESS (another agent's file, for example
# agent/.customer-harness) with AGAIN (the words that ask it to continue).
set -euo pipefail
cd "$(dirname "$0")/.."
A="aws --profile ${SMH_PROFILE:-poc-ino} --region ${SMH_REGION:-us-east-1} --no-cli-pager"; B="s3://smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}/build/src"
[ "$($A sts get-caller-identity --query Account --output text)" = "${SMH_ACCOUNT:-111122223333}" ] || { echo "STOP: wrong account"; exit 1; }
# The machines run the sources from the bucket: send the current ones first.
for d in image infra assure; do $A s3 sync --only-show-errors --delete --exact-timestamps --exclude "*__pycache__*" --exclude ".DS_Store" --exclude "*.pytest_cache*" "$d/" "$B/$d/"; done
$A s3 sync --only-show-errors --delete --exact-timestamps --exclude "*__pycache__*" sealed-model-poc/sealbox/ "$B/sealbox/"
mkdir -p evidence/m7
exec sealed-model-poc/.venv/bin/python deployer/run.py "$1" ${SESSION:+--session "$SESSION"} --rounds "${ROUNDS:-40}" --pause "${PAUSE:-120}" --out "${OUT:-evidence/m7/transcript.txt}" ${HARNESS:+--harness "$HARNESS"} ${AGAIN:+--again "$AGAIN"}

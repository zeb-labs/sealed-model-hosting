#!/usr/bin/env bash
# Create or update the two agents on AgentCore. Each is a stack (a Lambda function with narrow roles), a Gateway
# with one target, and a Harness (the agent loop) that may call only that Gateway.
#   the release agent  smh_poc_deployer   acts through fixed tools (deployer/tools.py)
#   the watcher        smh_poc_watcher    only reads, then decides (agent/fixer.py, agent/diagnose.py)
#   bash deployer/setup.sh            create or update both
#   bash deployer/setup.sh --delete   remove all of it
# Settings: MODEL, else SMH_AGENT_MODEL in smh.env, else an inference profile in .env: the Bedrock inference
# profile both agents think with. It must exist already; nothing is created for it.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f smh.env ]; then . ./smh.env; fi
PROFILE="${SMH_PROFILE:-poc-ino}"; ACCOUNT="${SMH_ACCOUNT:-111122223333}"; REGION="${SMH_REGION:-us-east-1}"; OWNER="${SMH_OWNER:-owner@example.com}"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"; C="$A bedrock-agentcore-control"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }
BUCKET="smh-poc-artifacts-$ACCOUNT"
MODEL="${MODEL:-$(python3 -c 'import sys; sys.path.insert(0, "agent"); import fixer; print(fixer.MODEL)')}"
# The fallback is a profile AWS itself provides. Not the newest model: its safety filter cut replies off (21 Nov).
MODEL="${MODEL:-global.anthropic.claude-opus-4-6-v1}"
case "$MODEL" in arn:*) MODEL_ARN="$MODEL";; *) MODEL_ARN="arn:aws:bedrock:$REGION:$ACCOUNT:inference-profile/$MODEL";; esac
STACK_TAGS="business-unit=ai env=innovation category=rnd owner=$OWNER app=Secure-model-hosting expire-date=recurring"
TAGS="${STACK_TAGS// /,}"
gw_id() { $C list-gateways --query "items[?name=='$1'].gatewayId | [0]" --output text; }
harness_id() { $C list-harnesses --query "harnesses[?harnessName=='$1'].harnessId | [0]" --output text; }
out() { $A cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }

#        stack                 gateway              harness           state file        steps
AGENTS=("smh-poc-m7-deployer smh-poc-deployer-gw smh_poc_deployer deployer/.harness 30"
        "smh-poc-m12-watcher smh-poc-watcher-gw  smh_poc_watcher  agent/.harness    14")

if [ "${1:-}" = "--delete" ]; then
  for agent in "${AGENTS[@]}"; do
    read -r STACK GW HARNESS STATE _ <<<"$agent"
    H="$(harness_id "$HARNESS")"; [ "$H" = None ] || { $C delete-harness --harness-id "$H" >/dev/null; echo "harness $HARNESS deleted"; }
    G="$(gw_id "$GW")"
    if [ "$G" != None ]; then
      for t in $($C list-gateway-targets --gateway-identifier "$G" --query 'items[].targetId' --output text); do $C delete-gateway-target --gateway-identifier "$G" --target-id "$t" >/dev/null; done
      sleep 20; $C delete-gateway --gateway-identifier "$G" >/dev/null; echo "gateway $GW deleted"
    fi
    $A cloudformation delete-stack --stack-name "$STACK"; $A cloudformation wait stack-delete-complete --stack-name "$STACK"; echo "deleted $STACK"; rm -f "$STATE"
  done
  exit 0
fi

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
for agent in "${AGENTS[@]}"; do
  read -r STACK GW HARNESS STATE STEPS <<<"$agent"
  echo "== $HARNESS"
  # 1. the function's code, named by its content so an update is picked up; and the agent's tools and instructions
  rm -f "$T"/*
  if [ "$HARNESS" = smh_poc_deployer ]; then
    cp deployer/tools.py deployer/handler.py "$T/"; TEMPLATE=infra/m7/deployer.yaml; EXTRA=(Owner="$OWNER")
    python3 -c 'import json,sys; sys.path.insert(0,"deployer"); import tools; json.dump(tools.SCHEMAS, open(sys.argv[1]+"/schemas.json","w")); json.dump([{"text": open("deployer/instructions.md").read()}], open(sys.argv[1]+"/prompt.json","w"))' "$T"
  else
    cp agent/fixer.py agent/diagnose.py "$T/"; TEMPLATE=infra/m12/watcher.yaml; EXTRA=(Owner="$OWNER")
    python3 -c 'import json,sys; sys.path.insert(0,"agent"); import fixer; json.dump(fixer.gateway_schemas(), open(sys.argv[1]+"/schemas.json","w")); json.dump([{"text": fixer.harness_prompt()}], open(sys.argv[1]+"/prompt.json","w"))' "$T"
  fi
  (cd "$T" && zip -q -X code.zip ./*.py)
  KEY="build/deployer/$(shasum -a 256 "$T/code.zip" | cut -c1-16).zip"
  $A s3 cp --only-show-errors "$T/code.zip" "s3://$BUCKET/$KEY"
  # shellcheck disable=SC2086
  $A cloudformation deploy --stack-name "$STACK" --template-file "$TEMPLATE" --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
    --parameter-overrides CodeKey="$KEY" ModelArn="$MODEL_ARN" ${EXTRA[@]+"${EXTRA[@]}"} --tags $STACK_TAGS

  # 2. the Gateway: callers sign with AWS credentials (no outside login service)
  G="$(gw_id "$GW")"
  if [ "$G" = None ]; then
    G="$($C create-gateway --name "$GW" --role-arn "$(out "$STACK" GatewayRoleArn)" --protocol-type MCP --authorizer-type AWS_IAM \
          --description "smh-poc ${HARNESS#smh_poc_} tools" --tags "$TAGS" --query gatewayId --output text)"
  fi
  for _ in $(seq 1 30); do [ "$($C get-gateway --gateway-identifier "$G" --query status --output text)" = READY ] && break; sleep 5; done
  python3 - "$(out "$STACK" ToolsArn)" "$T/schemas.json" > "$T/target.json" <<'PY'
import json, sys
def plain(schema):          # the Gateway's schema form has no "enum": say it in words instead
    out = {k: v for k, v in schema.items() if k != "enum"}
    if "enum" in schema:
        out["description"] = (schema.get("description", "") + " One of: " + ", ".join(schema["enum"]) + ".").strip()
    if "properties" in out:
        out["properties"] = {k: plain(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = plain(out["items"])
    return out
print(json.dumps({"mcp": {"lambda": {"lambdaArn": sys.argv[1], "toolSchema": {"inlinePayload": [
    {"name": s["name"], "description": s["description"], "inputSchema": plain(s["inputSchema"])} for s in json.load(open(sys.argv[2]))]}}}}))
PY
  TID="$($C list-gateway-targets --gateway-identifier "$G" --query "items[?name=='deploy'].targetId | [0]" --output text)"
  if [ "$TID" = None ]; then
    $C create-gateway-target --gateway-identifier "$G" --name deploy --target-configuration "file://$T/target.json" \
      --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' --query targetId --output text
  else
    $C update-gateway-target --gateway-identifier "$G" --target-id "$TID" --name deploy --target-configuration "file://$T/target.json" \
      --credential-provider-configurations '[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]' --query targetId --output text
  fi
  GARN="$($C get-gateway --gateway-identifier "$G" --query gatewayArn --output text)"

  # 3. the Harness: the agent loop. Only the Gateway's tools are allowed; the built-in shell and file tools are not.
  ARGS=(--execution-role-arn "$(out "$STACK" AgentRoleArn)" --model "{\"bedrockModelConfig\":{\"modelId\":\"$MODEL\",\"maxTokens\":4096}}"
        --system-prompt "file://$T/prompt.json"
        --tools "[{\"type\":\"agentcore_gateway\",\"name\":\"deploy\",\"config\":{\"agentCoreGateway\":{\"gatewayArn\":\"$GARN\"}}}]"
        --allowed-tools "@deploy/*" --max-iterations "$STEPS" --max-tokens 4096 --timeout-seconds 600)
  H="$(harness_id "$HARNESS")"
  if [ "$H" = None ]; then
    H="$($C create-harness --harness-name "$HARNESS" "${ARGS[@]}" --memory '{"disabled":{}}' --tags "$TAGS" --query harness.harnessId --output text)"
  else
    $C update-harness --harness-id "$H" "${ARGS[@]}" --memory '{"optionalValue":{"disabled":{}}}' >/dev/null   # on update, memory is wrapped
  fi
  for _ in $(seq 1 60); do S="$($C get-harness --harness-id "$H" --query harness.status --output text)"; [ "$S" = READY ] && break; case "$S" in *FAILED) echo "harness $S"; exit 1;; esac; sleep 5; done
  $C get-harness --harness-id "$H" --query 'harness.[harnessName,status,arn]' --output text | tee "$STATE"
  echo "gateway $G"
done

#!/usr/bin/env bash
# Create or update the deployer: the stack (Lambda, roles, the fixed document),
# then the AgentCore Gateway with its one target, then the AgentCore Harness.
#   bash deployer/setup.sh            create or update
#   bash deployer/setup.sh --delete   remove all of it
# Settings: MODEL (Bedrock inference profile the agent thinks with).
set -euo pipefail
cd "$(dirname "$0")/.."
PROFILE="poc-ino"; ACCOUNT="111122223333"; REGION="us-east-1"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"; C="$A bedrock-agentcore-control"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }
BUCKET="smh-poc-artifacts-$ACCOUNT"; STACK="smh-poc-m7-deployer"; GW="smh-poc-deployer-gw"; HARNESS="smh_poc_deployer"
# Not the newest model: its safety filter cut replies off on this key-and-security subject (smoke test, 21 Nov).
MODEL="${MODEL:-global.anthropic.claude-opus-4-6-v1}"
TAGS='business-unit=ai,env=innovation,category=rnd,owner=owner@example.com,app=Secure-model-hosting,expire-date=recurring'
gw_id() { $C list-gateways --query "items[?name=='$GW'].gatewayId | [0]" --output text; }
harness_id() { $C list-harnesses --query "harnesses[?harnessName=='$HARNESS'].harnessId | [0]" --output text; }

if [ "${1:-}" = "--delete" ]; then
  H="$(harness_id)"; [ "$H" = None ] || { $C delete-harness --harness-id "$H" >/dev/null; echo "harness deleted"; }
  G="$(gw_id)"
  if [ "$G" != None ]; then
    for t in $($C list-gateway-targets --gateway-identifier "$G" --query 'items[].targetId' --output text); do $C delete-gateway-target --gateway-identifier "$G" --target-id "$t" >/dev/null; done
    sleep 20; $C delete-gateway --gateway-identifier "$G" >/dev/null; echo "gateway deleted"
  fi
  $A cloudformation delete-stack --stack-name "$STACK"; $A cloudformation wait stack-delete-complete --stack-name "$STACK"; echo "deleted $STACK"; exit 0
fi

# 1. the function's code, named by its content so an update is picked up
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cp deployer/tools.py deployer/handler.py "$T/"; (cd "$T" && zip -q -X code.zip tools.py handler.py)
KEY="build/deployer/$(shasum -a 256 "$T/code.zip" | cut -c1-16).zip"
$A s3 cp --only-show-errors "$T/code.zip" "s3://$BUCKET/$KEY"
$A cloudformation deploy --stack-name "$STACK" --template-file infra/m7/deployer.yaml --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --parameter-overrides CodeKey="$KEY" ModelArn="arn:aws:bedrock:$REGION:$ACCOUNT:inference-profile/$MODEL" \
  --tags business-unit=ai env=innovation category=rnd owner=owner@example.com app=Secure-model-hosting expire-date=recurring
out() { $A cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

# 2. the Gateway: callers sign with AWS credentials (no outside login service)
G="$(gw_id)"
if [ "$G" = None ]; then
  G="$($C create-gateway --name "$GW" --role-arn "$(out GatewayRoleArn)" --protocol-type MCP --authorizer-type AWS_IAM \
        --description "smh-poc deployer tools" --tags "$TAGS" --query gatewayId --output text)"
fi
for _ in $(seq 1 30); do [ "$($C get-gateway --gateway-identifier "$G" --query status --output text)" = READY ] && break; sleep 5; done
python3 - "$(out ToolsArn)" > "$T/target.json" <<'PY'
import json, sys
sys.path.insert(0, "deployer")
import tools
def plain(schema):          # the Gateway's schema form has no "enum": say it in words instead
    out = {k: v for k, v in schema.items() if k != "enum"}
    if "enum" in schema:
        out["description"] = (schema.get("description", "") + " One of: " + ", ".join(schema["enum"]) + ".").strip()
    if "properties" in out:
        out["properties"] = {k: plain(v) for k, v in out["properties"].items()}
    return out
print(json.dumps({"mcp": {"lambda": {"lambdaArn": sys.argv[1], "toolSchema": {"inlinePayload": [
    {"name": s["name"], "description": s["description"], "inputSchema": plain(s["inputSchema"])} for s in tools.SCHEMAS]}}}}))
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
python3 -c 'import json,sys; print(json.dumps([{"text": open("deployer/instructions.md").read()}]))' > "$T/prompt.json"
ARGS=(--execution-role-arn "$(out AgentRoleArn)" --model "{\"bedrockModelConfig\":{\"modelId\":\"$MODEL\",\"maxTokens\":4096}}"
      --system-prompt "file://$T/prompt.json"
      --tools "[{\"type\":\"agentcore_gateway\",\"name\":\"deploy\",\"config\":{\"agentCoreGateway\":{\"gatewayArn\":\"$GARN\"}}}]"
      --allowed-tools "@deploy/*" --max-iterations 30 --max-tokens 4096 --timeout-seconds 600)
H="$(harness_id)"
if [ "$H" = None ]; then
  H="$($C create-harness --harness-name "$HARNESS" "${ARGS[@]}" --memory '{"disabled":{}}' --tags "$TAGS" --query harness.harnessId --output text)"
else
  $C update-harness --harness-id "$H" "${ARGS[@]}" --memory '{"optionalValue":{"disabled":{}}}' >/dev/null   # on update, memory is wrapped
fi
for _ in $(seq 1 60); do S="$($C get-harness --harness-id "$H" --query harness.status --output text)"; [ "$S" = READY ] && break; case "$S" in *FAILED) echo "harness $S"; exit 1;; esac; sleep 5; done
$C get-harness --harness-id "$H" --query 'harness.[harnessName,status,arn]' --output text | tee deployer/.harness
echo "gateway $G ($GARN)"

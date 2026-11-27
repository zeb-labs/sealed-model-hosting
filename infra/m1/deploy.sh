#!/usr/bin/env bash
# Deploy, update or remove the M1 stacks.
#   bash infra/m1/deploy.sh base              networks, bucket, roles
#   bash infra/m1/deploy.sh key               KMS key (after base)
#   PCR4=.. PCR7=.. PCR12=.. bash infra/m1/deploy.sh key    pin real PCR values
#   bash infra/m1/deploy.sh key --delete      remove (key first, then base)
#   PCR4=.. PCR15=<locked value> bash infra/m1/deploy.sh lease    the lease key (M5)
#   bash infra/m1/deploy.sh control           customer log group, Logs endpoint, flow logs (M5)
#   ALLOWED=true|false bash infra/m1/deploy.sh client    test client (M5; sealed machine must be running)
#   SOURCE=s3://bucket/folder HIBERNATE=true|false ... bash infra/m1/deploy.sh test    optional launch settings (M9)
#   bash infra/m1/deploy.sh ssm-endpoints     TEST ONLY: Systems Manager endpoints for suite test B7
set -euo pipefail
PROFILE="${SMH_PROFILE:-poc-ino}"; ACCOUNT="${SMH_ACCOUNT:-111122223333}"; REGION="${SMH_REGION:-us-east-1}"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"
DIR="$(cd "$(dirname "$0")" && pwd)"
WHICH="${1:?base or key}"; STACK="smh-poc-m1-$WHICH"
TEMPLATE="$WHICH"; [ "$WHICH" = "lease" ] && TEMPLATE="key"   # the lease key uses the key template
[ "$WHICH" = "bad" ] && TEMPLATE="test"     # a second sealed machine beside the good one (demo point 3)

ident="$($A sts get-caller-identity --query '[Account,Arn]' --output text)"
[ "${ident%%$'\t'*}" = "$ACCOUNT" ] || { echo "STOP: wrong account: $ident"; exit 1; }
USER_ARN="${ident##*$'\t'}"

if [ "${2:-}" = "--delete" ]; then
  $A cloudformation delete-stack --stack-name "$STACK"
  $A cloudformation wait stack-delete-complete --stack-name "$STACK"
  echo "deleted $STACK"; exit 0
fi

PARAMS=()
[ "$WHICH" = "gpu-helper" ] && PARAMS=(--parameter-overrides InstanceType="${TYPE:-g5.xlarge}")
[ "$WHICH" = "customer" ] && PARAMS=(--parameter-overrides CustomerAdminArn="$USER_ARN")
if [ "$WHICH" = "test" ] || [ "$WHICH" = "bad" ]; then
  PARAMS=(--parameter-overrides Ami="${AMI:?set AMI=ami-...}" InstanceType="${TYPE:-m6i.large}" Profile="${PROFILE_NAME:-smh-poc-sealed-instance-profile}" UserData="${USERDATA:-}" CrossAccountTag="${CROSS:-}" Name="smh-poc-sealed-$WHICH" ModelSource="${SOURCE:-}" Hibernate="${HIBERNATE:-false}")
fi
if [ "$WHICH" = "ssm-endpoints" ]; then   # TEST ONLY, suite test B7
  VPC="$($A ec2 describe-vpcs --filters Name=tag:Name,Values=smh-poc-customer-vpc Name=tag:app,Values=Secure-model-hosting --query 'Vpcs[0].VpcId' --output text)"
  ESG="$($A ec2 describe-security-groups --filters Name=vpc-id,Values=$VPC Name=tag:Name,Values=smh-poc-endpoint-sg --query 'SecurityGroups[0].GroupId' --output text)"
  PARAMS=(--parameter-overrides CustomerVpcId="$VPC" EndpointSgId="$ESG")
fi
if [ "$WHICH" = "control" ] || [ "$WHICH" = "client" ]; then
  VPC="$($A ec2 describe-vpcs --filters Name=tag:Name,Values=smh-poc-customer-vpc Name=tag:app,Values=Secure-model-hosting --query 'Vpcs[0].VpcId' --output text)"
  PARAMS=(--parameter-overrides CustomerVpcId="$VPC" CustomerBucket="smh-poc-customer-$ACCOUNT")
  if [ "$WHICH" = "client" ]; then
    # The sealed test machine's private address goes into the client's script.
    TARGET="$($A ec2 describe-instances --filters Name=tag:Name,Values=smh-poc-sealed-test Name=instance-state-name,Values=pending,running --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)"
    [ "$TARGET" != "None" ] || { echo "STOP: no running sealed test machine"; exit 1; }
    CKEY="$($A kms describe-key --key-id alias/smh-poc-customer-key --query KeyMetadata.Arn --output text)"
    # The customer's API key for the endpoint: made here, stored in the customer's bucket
    # (where the sealed machine reads it), handed to the client. Never printed.
    APIKEY="${API_KEY:-$(openssl rand -hex 24)}"
    printf '%s\n' "$APIKEY" | $A s3 cp --only-show-errors --sse aws:kms --sse-kms-key-id "$CKEY" - "s3://smh-poc-customer-$ACCOUNT/gate/api-keys.txt"
    S3PL="$($A ec2 describe-prefix-lists --filters "Name=prefix-list-name,Values=com.amazonaws.$REGION.s3" --query 'PrefixLists[0].PrefixListId' --output text)"
    PARAMS=(--parameter-overrides CustomerVpcId="$VPC" Allowed="${ALLOWED:-true}" CustomerBucket="smh-poc-customer-$ACCOUNT" CustomerKeyArn="$CKEY" S3PrefixList="$S3PL" UserData="$(sed -e "s/TARGET/$TARGET/" -e "s|CUSTBUCKET|smh-poc-customer-$ACCOUNT|" -e "s|CUSTKEY|$CKEY|" -e "s|APIKEY|$APIKEY|" "$DIR/client-test.sh")")
  fi
fi
if [ "$WHICH" = "key" ] || [ "$WHICH" = "lease" ]; then
  # Only the values you set are changed. CloudFormation keeps the others.
  PARAMS=(--parameter-overrides AdminUserArn="$USER_ARN")
  [ -n "${PCR4+x}" ] && PARAMS+=(Pcr4="$PCR4"); [ -n "${PCR7+x}" ] && PARAMS+=(Pcr7="$PCR7")
  [ "$WHICH" = "lease" ] && PARAMS+=(Alias=smh-poc-lease-key)
  [ -n "${PCR12+x}" ] && PARAMS+=(Pcr12="$PCR12"); [ -n "${PCR15+x}" ] && PARAMS+=(Pcr15="$PCR15")
  [ -n "${CUSTOMER_ROLE+x}" ] && PARAMS+=(CustomerRoleArn="$CUSTOMER_ROLE")   # M10: onboard one customer's machine role ("" removes it)
fi

# The six tags the SCP requires. Stack tags are copied onto every resource.
$A cloudformation deploy --stack-name "$STACK" --template-file "$DIR/$TEMPLATE.yaml" \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset ${PARAMS[@]+"${PARAMS[@]}"} \
  --tags business-unit=ai env=innovation category=rnd owner=${SMH_OWNER:-owner@example.com} app=Secure-model-hosting expire-date=recurring
# CloudFormation cannot tag instance profiles, so tag them here.
PROFILES=""; [ "$WHICH" = "base" ] && PROFILES="smh-poc-builder-profile smh-poc-sealed-instance-profile"; [ "$WHICH" = "client" ] && PROFILES="smh-poc-client-profile"
if [ -n "$PROFILES" ]; then
  for p in $PROFILES; do
    $A iam tag-instance-profile --instance-profile-name "$p" --tags Key=business-unit,Value=ai Key=env,Value=innovation Key=category,Value=rnd Key=owner,Value=${SMH_OWNER:-owner@example.com} Key=app,Value=Secure-model-hosting Key=expire-date,Value=recurring
  done
fi
$A cloudformation describe-stacks --stack-name "$STACK" --query 'Stacks[0].[StackStatus,Outputs]' --output json

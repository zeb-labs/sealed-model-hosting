#!/usr/bin/env bash
# Read-only access check for the secure model hosting POC.
# It only lists, describes, simulates and dry-runs.
# It creates nothing, changes nothing and deletes nothing.

PROFILE="poc-ino"
EXPECTED_ACCOUNT="111122223333"
REGION="us-east-1"

DIR="$(cd "$(dirname "$0")" && pwd)"
REPORT="$DIR/access-report.txt"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"

say() { echo "$@" | tee -a "$REPORT"; }
short() { tr '\n' ' ' | cut -c1-300; }

: > "$REPORT"
say "Access check, $(date -u +%Y-%m-%dT%H:%M:%SZ), profile $PROFILE, region $REGION"
say "aws cli: $(aws --version 2>&1)"

ACCOUNT="$($A sts get-caller-identity --query Account --output text 2>&1)"
ARN="$($A sts get-caller-identity --query Arn --output text 2>&1)"
say "account: $ACCOUNT"
say "identity: $ARN"
if [ "$ACCOUNT" != "$EXPECTED_ACCOUNT" ]; then
  say "STOP: this is not account $EXPECTED_ACCOUNT. Nothing else was run."
  exit 1
fi
USER_NAME="${ARN##*/}"

check() {
  label="$1"; shift
  out="$("$@" 2>&1)"; rc=$?
  if [ $rc -eq 0 ]; then say "OK    | $label | $(echo "$out" | short)"
  else say "FAIL  | $label | $(echo "$out" | short)"; fi
}

dry() {
  label="$1"; shift
  out="$("$@" 2>&1)"
  case "$out" in
    *DryRunOperation*)       say "OK    | $label | allowed (dry run, nothing created)";;
    *UnauthorizedOperation*) say "FAIL  | $label | not authorized";;
    *)                       say "CHECK | $label | $(echo "$out" | short)";;
  esac
}

say ""
say "== 1. Who this user is =="
check "attached policies"   $A iam list-attached-user-policies --user-name "$USER_NAME" --query 'AttachedPolicies[].PolicyName' --output text
check "inline policies"     $A iam list-user-policies --user-name "$USER_NAME" --query 'PolicyNames' --output text
check "groups"              $A iam list-groups-for-user --user-name "$USER_NAME" --query 'Groups[].GroupName' --output text
check "permission boundary" $A iam get-user --user-name "$USER_NAME" --query 'User.PermissionsBoundary.PermissionsBoundaryArn' --output text
for g in $($A iam list-groups-for-user --user-name "$USER_NAME" --query 'Groups[].GroupName' --output text 2>/dev/null); do
  check "group $g policies" $A iam list-attached-group-policies --group-name "$g" --query 'AttachedPolicies[].PolicyName' --output text
done
check "organization"        $A organizations describe-organization --query 'Organization.Id' --output text

say ""
say "== 2. Permission simulation (asks IAM, changes nothing) =="
for act in \
  ec2:RunInstances ec2:StopInstances ec2:TerminateInstances ec2:CreateVpc ec2:CreateSubnet \
  ec2:CreateSecurityGroup ec2:CreateVpcEndpoint ec2:CreateLaunchTemplate ec2:RegisterImage \
  ec2:CreateSnapshot ec2:ModifyImageAttribute ec2:CreateTags \
  ebs:StartSnapshot ebs:PutSnapshotBlock ebs:CompleteSnapshot \
  kms:CreateKey kms:PutKeyPolicy kms:GenerateDataKey kms:Decrypt kms:DisableKey kms:ScheduleKeyDeletion \
  s3:CreateBucket s3:PutObject s3:GetObject s3:PutBucketPolicy \
  iam:CreateRole iam:PutRolePolicy iam:AttachRolePolicy iam:CreateInstanceProfile iam:PassRole \
  logs:CreateLogGroup logs:PutLogEvents cloudtrail:CreateTrail cloudtrail:LookupEvents \
  ecr:CreateRepository route53:CreateHostedZone ssm:StartSession ssm:SendCommand \
  cloudformation:CreateStack servicequotas:RequestServiceQuotaIncrease budgets:ModifyBudget \
  bedrock:InvokeModel bedrock-agentcore:CreateAgentRuntime bedrock-agentcore:InvokeAgentRuntime
do
  res="$($A iam simulate-principal-policy --policy-source-arn "$ARN" --action-names "$act" --query 'EvaluationResults[0].EvalDecision' --output text 2>&1)"
  say "SIM   | $act | $(echo "$res" | tr '\n' ' ' | cut -c1-160)"
done

say ""
say "== 3. Read access to each service (counts only, no names) =="
check "ec2 running instances" $A ec2 describe-instances --filters Name=instance-state-name,Values=running --query 'length(Reservations[].Instances[])' --output text
check "ec2 vpcs"              $A ec2 describe-vpcs --query 'length(Vpcs)' --output text
check "ec2 own images"        $A ec2 describe-images --owners self --query 'length(Images)' --output text
check "kms keys"              $A kms list-keys --query 'length(Keys)' --output text
check "s3 buckets"            $A s3api list-buckets --query 'length(Buckets)' --output text
check "log groups"            $A logs describe-log-groups --query 'length(logGroups)' --output text
check "ecr repositories"      $A ecr describe-repositories --query 'length(repositories)' --output text
check "cloudformation stacks" $A cloudformation list-stacks --query 'length(StackSummaries)' --output text
check "route53 zones"         $A route53 list-hosted-zones --query 'length(HostedZones)' --output text
check "cloudtrail trails"     $A cloudtrail describe-trails --query 'length(trailList)' --output text
check "budgets"               $A budgets describe-budgets --account-id "$ACCOUNT" --query 'length(Budgets)' --output text

say ""
say "== 4. GPU machine: quota, availability, security chip support =="
check "quota G and VT (vCPUs, need 64)" $A service-quotas list-service-quotas --service-code ec2 --query "Quotas[?contains(QuotaName, 'G and VT')].[QuotaName,Value]" --output text
check "g5.8xlarge zones"   $A ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=instance-type,Values=g5.8xlarge --query 'InstanceTypeOfferings[].Location' --output text
check "g5.8xlarge details (tpm, boot modes, vcpu, ram MiB, gpu MiB)" $A ec2 describe-instance-types --instance-types g5.8xlarge --query 'InstanceTypes[0].[NitroTpmSupport,SupportedBootModes,VCpuInfo.DefaultVCpus,MemoryInfo.SizeInMiB,GpuInfo.TotalGpuMemoryInMiB]' --output json

say ""
say "== 5. Dry runs (permission test only, nothing is created) =="
AMI="$($A ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text 2>/dev/null)"
if [ -n "$AMI" ] && [ "$AMI" != "None" ]; then
  dry "launch a g5.8xlarge" $A ec2 run-instances --dry-run --image-id "$AMI" --instance-type g5.8xlarge --count 1
else
  say "CHECK | launch a g5.8xlarge | could not look up a public Amazon Linux image id"
fi
dry "create a vpc" $A ec2 create-vpc --dry-run --cidr-block 10.99.0.0/16

say ""
say "== 6. Bedrock and AgentCore =="
check "bedrock anthropic models visible" $A bedrock list-foundation-models --by-provider anthropic --query 'length(modelSummaries)' --output text
check "agentcore runtimes list"          $A bedrock-agentcore-control list-agent-runtimes --output json

say ""
say "Done. Report saved to $REPORT"

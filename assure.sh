#!/usr/bin/env bash
# The assurance suite: attack a finished build and write a report.
#   bash assure.sh static      read-only checks, no machines, free
#   bash assure.sh live        one good boot with a client, ends with a cut-off (GPU, about 30 minutes)
#   bash assure.sh break       eight attacks that must each fail (GPU, about 2 hours); BREAKS="B5 B6" runs only those
#   bash assure.sh all         the three tiers, then the report
#   bash assure.sh recheck     only the good boot that ends the break tier (proves swapped objects are back)
#   bash assure.sh report      judge an existing run again: RUN=evidence/m6/<run> bash assure.sh report
# Settings: VERSION (default 0.6.0), BUILD (default evidence/m6/build-$VERSION,
# holding summary.json, ami-id.txt, pipeline.log and, for "break", breaks.json),
# RUN (default evidence/m6/<UTC date-time>).
#
# live and break START MACHINES AND COST MONEY, disable and re-enable our own
# two keys, and (break) swap two of our own bucket objects and put them back.
# Whatever happens, the exit handler enables the keys, restores the objects
# and deletes the test machines. Evidence is judged by assure/report.py only.
set -uo pipefail
cd "$(dirname "$0")"
PROFILE="poc-ino"; ACCOUNT="111122223333"; REGION="us-east-1"
A="aws --profile $PROFILE --region $REGION --no-cli-pager"
[ "$($A sts get-caller-identity --query Account --output text)" = "$ACCOUNT" ] || { echo "STOP: wrong account"; exit 1; }

TIER="${1:?static, live, break, all or report}"
VERSION="${VERSION:-0.6.0}"; BUILD="${BUILD:-evidence/m6/build-$VERSION}"
RUN="${RUN:-evidence/m6/$(date -u +%Y%m%d-%H%M)}"
PY="sealed-model-poc/.venv/bin/python"; D="infra/m1"
ART="smh-poc-artifacts-$ACCOUNT"; CUST="smh-poc-customer-$ACCOUNT"
CUST_KEY="$($A kms describe-key --key-id alias/smh-poc-customer-key --query KeyMetadata.Arn --output text)"
MODEL_KEY="$($A kms describe-key --key-id alias/smh-poc-model-key --query KeyMetadata.KeyId --output text)"
LEASE_KEY="$($A kms describe-key --key-id alias/smh-poc-lease-key --query KeyMetadata.KeyId --output text)"
TYPES="${TYPES:-g5.xlarge g6.xlarge g5.2xlarge}"
INTERVAL=60; REFUSALS=2          # must match image/m5/loader.json
say() { echo "[$(date -u +%H:%M:%S)] $*"; }

if [ "$TIER" = "report" ]; then $PY assure/report.py "$RUN"; exit $?; fi
[ -f "$BUILD/summary.json" ] && [ -f "$BUILD/ami-id.txt" ] || { echo "STOP: $BUILD needs summary.json and ami-id.txt"; exit 1; }
AMI="$(cut -d' ' -f1 "$BUILD/ami-id.txt")"
mkdir -p "$RUN"
[ -f "$RUN/run.json" ] || echo "{\"ami\": \"$AMI\", \"version\": \"$VERSION\", \"started\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"tiers\": \"$TIER\"}" > "$RUN/run.json"
START_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"; START_MS="$(($(date -u +%s) * 1000))"

# ------------------------------------------------------------------ helpers

state() { $A ec2 describe-instances --instance-ids "$1" --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo gone; }
stack_instance() { $A cloudformation describe-stacks --stack-name "smh-poc-m1-$1" --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text 2>/dev/null; }
console() { $A ec2 get-console-output --instance-id "$1" --latest --query Output --output text > "$2" 2>/dev/null || true; }

launch() {   # launch <ami> [profile]; other inputs through env. Prints the instance id, or nothing.
  local t
  for t in $TYPES; do
    if TYPE="$t" AMI="$1" PROFILE_NAME="${2:-smh-poc-sealed-instance-profile}" bash $D/deploy.sh test >"$RUN/last-launch.log" 2>&1; then
      stack_instance test; return 0
    fi
    bash $D/deploy.sh test --delete >/dev/null 2>&1
  done
  return 1
}

wait_for() {   # wait_for <instance> <console file> <seconds> <pattern>: until the pattern shows or the machine stops
  local end=$(( $(date +%s) + $3 ))
  while [ "$(date +%s)" -lt "$end" ]; do
    console "$1" "$2"
    grep -qE "$4" "$2" && return 0
    case "$(state "$1")" in stopped|stopping|terminated|gone) console "$1" "$2"; return 0;; esac
    sleep 15
  done
  return 1
}

RESTORE=()   # "backup-key original-key" pairs still to put back
restore_objects() {
  local pair
  for pair in ${RESTORE[@]+"${RESTORE[@]}"}; do
    $A s3 cp --only-show-errors "s3://$ART/${pair%% *}" "s3://$ART/${pair##* }" && say "restored ${pair##* }"
  done
  RESTORE=()
}

cleanup() {
  say "cleanup: keys on, objects back, test machines deleted"
  $A kms enable-key --key-id "$MODEL_KEY"; $A kms enable-key --key-id "$LEASE_KEY"
  restore_objects
  for s in client test ssm-endpoints; do
    $A cloudformation describe-stacks --stack-name "smh-poc-m1-$s" >/dev/null 2>&1 && bash $D/deploy.sh $s --delete >/dev/null 2>&1
  done
  local v; for v in ${TEMP_AMIS[@]+"${TEMP_AMIS[@]}"}; do $A ec2 deregister-image --image-id "$v" >/dev/null 2>&1; done
}
TEMP_AMIS=()
unset SOURCE HIBERNATE   # launch settings are given per test, never inherited from the caller
[ "$TIER" = "static" ] || trap cleanup EXIT

control() {   # the customer's log group, Logs endpoint and flow logs must exist before a boot
  $A cloudformation describe-stacks --stack-name smh-poc-m1-control >/dev/null 2>&1 || { say "creating the control stack"; bash $D/deploy.sh control >/dev/null; }
}

# ------------------------------------------------------------------ tier 1

static() {
  say "tier 1: static checks"
  mkdir -p "$RUN/static"
  { (cd sealed-model-poc && .venv/bin/python -m pytest -q 2>&1 | tail -3); (cd assure && ../$PY -m pytest -q . 2>&1 | tail -3); } > "$RUN/static/unit-tests.txt"
  $PY assure/collect.py static "$RUN" "$AMI" "$BUILD"
}

# ------------------------------------------------------------------ tier 2

live() {
  say "tier 2: live run on $AMI"
  local L="$RUN/live"; mkdir -p "$L/jobs"
  # Launched the way a hostile customer would: a start-up script and the AWS sample's tag.
  # LIVE_SOURCE=s3://<customer bucket>/model/<folder>: load the release from the customer's copy (M9).
  local I; I="$(SOURCE="${LIVE_SOURCE:-}" USERDATA=$'#!/bin/bash\necho SMH-USERDATA-RAN > /dev/console\n' CROSS=111122223333 launch "$AMI")" || { say "NOT RUN: no GPU capacity"; return 1; }
  say "sealed machine $I; waiting for the boot (about 12 minutes)"
  wait_for "$I" "$L/boot-console.txt" 1500 "SMH DONE" || say "boot did not finish in time"
  ALLOWED=true bash $D/deploy.sh client >/dev/null 2>&1 || say "client did not start"
  local C; C="$(stack_instance client)"
  local RID="a$(date -u +%m%d%H%M)"; local bad=""
  cp image/m4/sample/job-good.json "$L/good-form.json"
  $A s3 cp --only-show-errors image/m4/sample/job-good.json "s3://$CUST/jobs/$RID-good.json" --sse aws:kms --sse-kms-key-id "$CUST_KEY"
  local f n
  for f in image/m4/sample/job-bad-*.json; do
    n="$RID-$(basename "$f" .json | sed 's/^job-//')"; bad="$bad \"$n\","
    $A s3 cp --only-show-errors "$f" "s3://$CUST/jobs/$n.json" --sse aws:kms --sse-kms-key-id "$CUST_KEY"
  done
  echo "{\"good_job\": \"$RID-good\", \"bad_jobs\": [${bad%,}], \"boots\": 1}" > "$L/ids.json"
  say "7 job forms submitted as $RID-*; waiting for results and for usage records (7 minutes)"
  sleep 420
  for n in $($A s3 ls "s3://$CUST/results/" | awk '{print $2}' | tr -d / | grep "^$RID-"); do
    mkdir -p "$L/jobs/$n"; $A s3 cp --only-show-errors "s3://$CUST/results/$n/status.json" "$L/jobs/$n/status.json"
  done
  $A s3api list-objects-v2 --bucket "$CUST" --prefix "adapters/$RID-good/" --query 'Contents[].[Key,Size]' --output text \
    | $PY -c 'import sys,json; print(json.dumps({l.split()[0].rsplit("/",1)[1]: int(l.split()[1]) for l in sys.stdin if l.strip() and l.split()[0] != "None"}))' > "$L/adapter-listing.json"
  console "$C" "$L/client-console.txt"
  $PY assure/collect.py live "$RUN" "$I" "$C" "$START_MS"

  say "cut-off: disabling both keys"
  $A kms disable-key --key-id "$LEASE_KEY"; $A kms disable-key --key-id "$MODEL_KEY"
  local T0; T0="$(date -u +%H:%M:%S)"
  local end=$(( $(date +%s) + 600 ))
  while [ "$(date +%s)" -lt "$end" ] && [ "$(state "$I")" = running ]; do sleep 10; done
  local FINAL; FINAL="$(state "$I")"
  $A kms enable-key --key-id "$MODEL_KEY"; $A kms enable-key --key-id "$LEASE_KEY"
  sleep 20; console "$C" "$L/client-console-after-cutoff.txt"; console "$I" "$L/sealed-console-after-cutoff.txt"
  echo "{\"disabled_at\": \"$T0\", \"final_state\": \"$FINAL\", \"bound_seconds\": $((REFUSALS * INTERVAL + 60))}" > "$L/cutoff.json"
  say "machine is $FINAL"

  # The builder's view, and the disk comparison, both on the builder machine.
  local B; B="$(stack_instance builder)"; local was; was="$(state "$B")"
  [ "$was" = running ] || { $A ec2 start-instances --instance-ids "$B" >/dev/null; $A ec2 wait instance-status-ok --instance-ids "$B"; }
  bash $D/ssm.sh 'bash /opt/smh/src/image/pipeline/check-usage.sh tamper' > "$L/usage-check.txt" 2>&1 || true
  $A ec2 wait instance-stopped --instance-ids "$I" 2>/dev/null
  disk_compare "$I" "$B" "$L"
  [ "$was" = running ] || $A ec2 stop-instances --instance-ids "$B" >/dev/null
  bash $D/deploy.sh client --delete >/dev/null 2>&1; bash $D/deploy.sh test --delete >/dev/null 2>&1
  say "waiting 8 minutes for CloudTrail to deliver"; sleep 480
  $PY assure/collect.py trail "$L" "$START_ISO"
}

disk_compare() {   # the stopped machine's system disk must equal the released image
  local I="$1" B="$2" L="$3" T="{Key=business-unit,Value=ai},{Key=env,Value=innovation},{Key=category,Value=rnd},{Key=owner,Value=owner@example.com},{Key=app,Value=Secure-model-hosting},{Key=expire-date,Value=recurring}"
  local ROOTVOL SNAP VOL AZ
  ROOTVOL="$($A ec2 describe-instances --instance-ids "$I" --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)"
  AZ="$($A ec2 describe-instances --instance-ids "$B" --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)"
  SNAP="$($A ec2 create-snapshot --volume-id "$ROOTVOL" --description "smh-poc assure: system disk after a run" \
    --tag-specifications "ResourceType=snapshot,Tags=[{Key=Name,Value=smh-poc-assure-disk},$T]" --query SnapshotId --output text)" || return 1
  local i; for i in 1 2 3 4; do $A ec2 wait snapshot-completed --snapshot-ids "$SNAP" && break; done
  VOL="$($A ec2 create-volume --availability-zone "$AZ" --snapshot-id "$SNAP" --volume-type gp3 \
    --tag-specifications "ResourceType=volume,Tags=[{Key=Name,Value=smh-poc-assure-disk},$T]" --query VolumeId --output text)"
  $A ec2 wait volume-available --volume-ids "$VOL"
  $A ec2 attach-volume --volume-id "$VOL" --instance-id "$B" --device /dev/sdg >/dev/null; $A ec2 wait volume-in-use --volume-ids "$VOL"
  TIMEOUT=3600 bash $D/ssm.sh "
set -e
serial=${VOL/-/}
for i in \$(seq 1 30); do dev=\$(lsblk -dn -o NAME,SERIAL | awk -v s=\$serial '\$2==s{print \$1}'); [ -n \"\$dev\" ] && break; sleep 2; done
raw=\$(ls /mnt/m5/signed/*.raw); size=\$(stat -c %s \$raw)
echo IMAGE \$(sha256sum \$raw | cut -c1-64)
echo DISK \$(head -c \$size /dev/\$dev | sha256sum | cut -c1-64)
echo TAIL \$(tail -c +\$((size+1)) /dev/\$dev | tr -d '\\0' | wc -c)
echo DIFFBYTES \$(cmp -l -n \$size \$raw /dev/\$dev 2>/dev/null | head -n 2000000 | wc -l)
cmp -l -n \$size \$raw /dev/\$dev 2>/dev/null | head -n 2000000 | awk '{print int((\$1-1)/1048576)}' | uniq -c | head -40 | sed 's/^/DIFFMIB /'
sfdisk -l -o Device,Start,Sectors,Type /dev/\$dev 2>/dev/null | sed 's/^/PART /'
" > "$L/disk-compare.txt" 2>&1 || true
  $PY -c 'import sys,re,json; t=open(sys.argv[1]).read(); g=lambda k: (re.search(k+r" ([0-9a-f]{64})",t) or [None,""])[1]; m=re.search(r"TAIL (\d+)",t); d=re.search(r"DIFFBYTES (\d+)",t); print(json.dumps({"image_sha": g("IMAGE"), "disk_sha": g("DISK"), "tail_nonzero": int(m[1]) if m else None, "diff_bytes": int(d[1]) if d else None, "diff_mib": [int(x) for x in re.findall(r"DIFFMIB\s+\d+ (\d+)",t)]}))' "$L/disk-compare.txt" > "$L/disk.json"
  $A ec2 detach-volume --volume-id "$VOL" >/dev/null; $A ec2 wait volume-available --volume-ids "$VOL"
  $A ec2 delete-volume --volume-id "$VOL"; $A ec2 delete-snapshot --snapshot-id "$SNAP"
}

# ------------------------------------------------------------------ tier 3

one_break() {   # one_break <id> <ami> <profile> <wait seconds> [with-client]
  local id="$1" dir="$RUN/break/$1"; mkdir -p "$dir"
  say "break $id"
  local I; I="$(launch "$2" "$3")" || { say "NOT RUN $id: no GPU capacity"; return 1; }
  echo "$I" > "$dir/instance.txt"
  if [ "${5:-}" = with-client ]; then ALLOWED=true bash $D/deploy.sh client >/dev/null 2>&1; fi
  wait_for "$I" "$dir/console.txt" "$4" "SMH DONE|Kernel panic - not syncing|No bootable device" || true
  sleep 150   # room for the lease refusals and the power-off
  console "$I" "$dir/console.txt"; state "$I" > "$dir/state.txt"
  if [ "${5:-}" = with-client ]; then console "$(stack_instance client)" "$dir/client-console.txt"; bash $D/deploy.sh client --delete >/dev/null 2>&1; else : > "$dir/client-console.txt"; fi
  bash $D/deploy.sh test --delete >/dev/null 2>&1
  say "break $id: machine was $(cat "$dir/state.txt")"
}

hibernate_test() {   # B8: a machine type that CAN hibernate (GPU types cannot), launched with hibernation asked for
  local dir="$RUN/break/B8"; mkdir -p "$dir"
  say "break B8"
  local I; I="$(TYPES="${HIB_TYPE:-m6id.large}" HIBERNATE=true launch "$AMI")" || { tail -c 600 "$RUN/last-launch.log" > "$dir/launch-refused.txt"; say "NOT RUN B8: launch refused"; return 1; }
  echo "$I" > "$dir/instance.txt"
  wait_for "$I" "$dir/console.txt" 600 "SMH DONE|KEY_RELEASED|key release failed" || true
  sleep 30; console "$I" "$dir/console-before.txt"
  $A ec2 stop-instances --instance-ids "$I" --hibernate > "$dir/hibernate.txt" 2>&1 || true
  local end=$(( $(date +%s) + 600 )); while [ "$(date +%s)" -lt "$end" ] && [ "$(state "$I")" != stopped ]; do sleep 10; done
  [ "$(state "$I")" = stopped ] || { $A ec2 stop-instances --instance-ids "$I" --force >/dev/null 2>&1; $A ec2 wait instance-stopped --instance-ids "$I"; echo "FORCED" >> "$dir/hibernate.txt"; }
  console "$I" "$dir/console.txt"; state "$I" > "$dir/state.txt"
  $A ec2 describe-instances --instance-ids "$I" --query 'Reservations[0].Instances[0].[StateTransitionReason,StateReason.Message]' --output text >> "$dir/hibernate.txt" 2>&1
  local B; B="$(stack_instance builder)"; local was; was="$(state "$B")"
  [ "$was" = running ] || { $A ec2 start-instances --instance-ids "$B" >/dev/null; $A ec2 wait instance-status-ok --instance-ids "$B"; }
  disk_compare "$I" "$B" "$dir"
  [ "$was" = running ] || $A ec2 stop-instances --instance-ids "$B" >/dev/null
  bash $D/deploy.sh test --delete >/dev/null 2>&1
}

swap() {   # swap <original key> <replacement key>: keep a copy of the original inside the bucket first
  local backup="m6-backup/$(basename "$1")"
  $A s3 cp --only-show-errors "s3://$ART/$1" "s3://$ART/$backup" || return 1
  local a b
  a="$($A s3api head-object --bucket "$ART" --key "$1" --query ContentLength --output text)"
  b="$($A s3api head-object --bucket "$ART" --key "$backup" --query ContentLength --output text)"
  [ "$a" = "$b" ] || { say "backup of $1 has the wrong size; not swapping"; return 1; }
  RESTORE+=("$backup $1")
  $A s3 cp --only-show-errors "s3://$ART/$2" "s3://$ART/$1"
}

want() { case " ${BREAKS:-B1 B2 B3 B4 B5 B6 B7 B8} " in *" $1 "*) return 0;; esac; return 1; }   # BREAKS="B5 B6": only those

breaks() {
  say "tier 3: break runs (${BREAKS:-all six})"
  local B2="" B3="" SNAP PREFIX
  if want B2 || want B3; then
    [ -f "$BUILD/breaks.json" ] || { say "NOT RUN: $BUILD/breaks.json missing (run image/pipeline/build-breaks.sh and register the images)"; return 1; }
    B2="$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["B2"])' "$BUILD/breaks.json")"
    B3="$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["B3"])' "$BUILD/breaks.json")"
  fi
  # The bucket folder of THIS build: from its install package if it has one (deployer builds), else the recipe's default.
  PREFIX="$($PY -c 'import json,sys,os; p=sys.argv[1]+"/customer-install/install.json"; print(json.load(open(p))["folder"] if os.path.exists(p) else json.load(open("image/m5/loader.json"))["prefix"])' "$BUILD")"

  # B1: the same disk, registered WITHOUT Secure Boot data.
  SNAP="$(cut -d' ' -f2 "$BUILD/ami-id.txt")"
  local B1=""; want B1 && B1="$($A ec2 register-image --name "smh-poc-break-b1-$(date -u +%Y%m%d%H%M%S)" --root-device-name /dev/xvda --architecture x86_64 \
    --virtualization-type hvm --boot-mode uefi --tpm-support v2.0 --ena-support \
    --block-device-mappings "DeviceName=/dev/xvda,Ebs={SnapshotId=$SNAP,VolumeType=gp3,DeleteOnTermination=true}" \
    --tag-specifications "ResourceType=image,Tags=[{Key=Name,Value=smh-poc-break-b1},{Key=business-unit,Value=ai},{Key=env,Value=innovation},{Key=category,Value=rnd},{Key=owner,Value=owner@example.com},{Key=app,Value=Secure-model-hosting},{Key=expire-date,Value=recurring}]" \
    --query ImageId --output text)" && TEMP_AMIS+=("$B1")
  [ -n "${B1:-}" ] && want B1 && one_break B1 "$B1" smh-poc-sealed-instance-profile 600
  want B2 && one_break B2 "$B2" smh-poc-sealed-instance-profile 600
  want B3 && one_break B3 "$B3" smh-poc-sealed-instance-profile 300
  want B4 && one_break B4 "$AMI" smh-poc-builder-profile 600
  # B5: another (valid, encrypted) object in place of the container archive.
  if want B5 && swap "$PREFIX/model-server.sbox" "$PREFIX/model-config.sbox"; then one_break B5 "$AMI" smh-poc-sealed-instance-profile 600; restore_objects; fi
  # B6: the weights with one changed byte (prepared by build-breaks.sh).
  if want B6 && swap "$PREFIX/model.tensors.sbox" "m6/model.tensors.tampered.sbox"; then one_break B6 "$AMI" smh-poc-sealed-instance-profile 1500 with-client; restore_objects; fi
  # B7: the customer gives the machine a role with Systems Manager rights and adds the endpoints the agent needs.
  if want B7; then
    bash $D/deploy.sh ssm-endpoints >/dev/null 2>&1 || say "B7: Systems Manager endpoints did not deploy"
    one_break B7 "$AMI" smh-poc-builder-profile 600
    $A ssm describe-instance-information --query 'InstanceInformationList[].InstanceId' --output json > "$RUN/break/B7/ssm.json" 2>/dev/null || true
    $A ec2 describe-vpc-endpoints --filters Name=tag:aws:cloudformation:stack-name,Values=smh-poc-m1-ssm-endpoints --query 'VpcEndpoints[].ServiceName' --output json > "$RUN/break/B7/endpoints.json" 2>/dev/null || true
    bash $D/deploy.sh ssm-endpoints --delete >/dev/null 2>&1
  fi
  want B8 && hibernate_test
  recheck
  say "waiting 8 minutes for CloudTrail to deliver"; sleep 480
  local id; for id in B1 B2 B3 B4 B5 B6 B7; do [ -d "$RUN/break/$id" ] && $PY assure/collect.py trail "$RUN/break/$id" "$START_ISO"; done
}

recheck() {   # a plain good boot: proves the swapped objects are back as they were
  say "restore check: one good boot"
  local dir="$RUN/break/R1"; mkdir -p "$dir"
  local I; I="$(launch "$AMI")" || { say "NOT RUN R1: no GPU capacity"; return 1; }
  wait_for "$I" "$dir/console.txt" 1500 "SMH DONE" || say "boot did not finish in time"
  bash $D/deploy.sh test --delete >/dev/null 2>&1
}

case "$TIER" in
  recheck) control; recheck;;
  static) static;;
  live) control; static; live;;
  break) control; breaks;;
  all) control; static; live; breaks;;
  *) echo "unknown tier: $TIER"; exit 2;;
esac
$PY assure/report.py "$RUN"

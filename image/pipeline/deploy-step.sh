#!/bin/bash
# The ONE entry point the deployer can run on our machines (through the
# Systems Manager document smh-poc-pipeline-step, which checks every input
# against a list or a pattern). Runs a fixed step and writes a result file
# with FIXED FIELDS to s3://.../deploy/<version>/<step>.json. The full log
# goes to deploy/<version>/logs/, which the agent's roles cannot read.
#   deploy-step.sh <step> <version> <prefix> <model> <gpu>
set -uo pipefail
STEP="$1"; VERSION="$2"; PREFIX="$3"; MODEL="$4"; GPU="$5"
# The build machine works in its own account, whichever that is: every pipeline script below reads this.
export SMH_ACCOUNT="${SMH_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo 111122223333)}"
REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}; SRC=/opt/smh/src; P=$SRC/image/pipeline
OUT="s3://$BUCKET/deploy/$VERSION"; LOG="/var/log/smh-deploy-$VERSION-$STEP.log"; WORK=/mnt/m5
export PYTHONPATH=$SRC VERSION PREFIX MODEL WORK ART=artifacts.json
s3get() { aws s3 cp --only-show-errors --region $REGION "$1" "$2"; }

case "$STEP" in
  lock)     bash $P/lock-artifacts.sh >"$LOG" 2>&1 ;;
  build)    mkdir -p $WORK && bash $P/build-m5.sh >"$LOG" 2>&1 ;;
  register) { bash $P/register-image.sh | tee /tmp/register.out \
              && awk '/^AMI /{print $2, $4}' /tmp/register.out | aws s3 cp --only-show-errors --region $REGION - "s3://$BUCKET/build/out-$VERSION/ami-id.txt"; } >"$LOG" 2>&1 ;;
  check)    bash $P/check-static.sh >"$LOG" 2>&1 ;;
  package)  bash $P/customer-package.sh "$GPU" >"$LOG" 2>&1 ;;
  pin)      bash $P/pin-keys.sh >"$LOG" 2>&1 ;;
  setup)    bash $P/bootstrap-builder.sh >"$LOG" 2>&1 ;;
  *)        echo "unknown step" >"$LOG"; false ;;
esac
RC=$?

# Result: fixed fields only, taken from the pipeline's own output files, never from log text.
python3 - "$STEP" "$VERSION" "$RC" "$WORK" "$PREFIX" <<'PY' > /tmp/result.json
import hashlib, json, os, re, sys
step, version, rc, work, prefix = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], sys.argv[5]
r = {"step": step, "version": version, "ok": rc == 0, "exit": rc}
def load(path):
    try: return json.load(open(path))
    except Exception: return {}
if step == "build" and rc == 0:
    raw = open(f"{work}/out/summary.json", "rb").read(); s = json.loads(raw)
    r.update(pcr4=s["PCR4"], pcr7=s["PCR7"], pcr12=s["PCR12"], pcr15_open=s["PCR15_open"], pcr15_locked=s["PCR15_locked"],
             summary_sha256=hashlib.sha256(raw).hexdigest(),
             packages=sum(1 for _ in open([f"{work}/out/{f}" for f in os.listdir(f"{work}/out") if f.endswith(".packages")][0])),
             image_check="OK" if "CHECK-IMAGE OK" in open(f"/var/log/smh-deploy-{version}-build.log", errors="replace").read() else "MISSING")
if step == "register" and rc == 0:
    m = re.search(r"^AMI (ami-[0-9a-f]+) SNAPSHOT (snap-[0-9a-f]+)$", open("/tmp/register.out").read(), re.M)
    r.update(ami=m.group(1), snapshot=m.group(2)) if m else r.update(ok=False)
if step == "check":
    r["checks"] = {x["id"]: x["verdict"] for x in load("/mnt/m7-check/results.json") if x["id"].startswith("S")} if os.path.exists("/mnt/m7-check/results.json") else {}
if step == "lock" and rc == 0:
    r["sizes"] = load("/data/artifacts-uploaded.json").get("sizes", {})
if step == "setup":       # only values of a fixed shape get through
    s = load("/var/lib/smh-setup.json") if rc == 0 else {}
    r.update({k: v for k, v in s.items() if k in ("ready", "keys", "kernel", "driver", "packages", "lease_secret", "disk_free_gib")
              and (isinstance(v, (bool, int)) or re.fullmatch(r"[A-Za-z0-9._-]{1,60}", str(v)))})
if step == "pin" and rc == 0:
    r["stacks"] = ["smh-poc-m1-key", "smh-poc-m1-lease"]
print(json.dumps(r))
PY
aws s3 cp --only-show-errors --region $REGION "$LOG" "$OUT/logs/$STEP.log"
aws s3 cp --only-show-errors --region $REGION /tmp/result.json "$OUT/$STEP.json"
exit $RC

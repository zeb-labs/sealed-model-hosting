#!/bin/bash
# GPU helper, phase 2: run the sealed trainer exactly as the job runner will
# (no network, unprivileged user, read-only), then run the output checker.
set -uo pipefail
cd /data
python3 /data/src/m4/params.py /data/base | tee /data/params.json
JOB=${1:-zebby-1}; OUT=/data/adapters/$JOB; rm -rf $OUT; mkdir -p $OUT; chmod 777 $OUT
cp /data/src/m4/sample/data.jsonl /data/job/data.jsonl
python3 -c "
import json; f=json.load(open('/data/src/m4/sample/job-good.json'))
json.dump({k:f[k] for k in ('rank','alpha','learning_rate','max_steps','target_modules','seed')}, open('/data/job/form.json','w'))"
T0=$(date +%s)
docker run --rm --name trainer --network none --runtime=nvidia --gpus all \
  --read-only --tmpfs /tmp:exec --cap-drop ALL --security-opt no-new-privileges \
  --user 65534:65534 -e HOME=/tmp -e HF_HUB_OFFLINE=1 \
  -v /data/base:/base:ro -v /data/job/data.jsonl:/job/data.jsonl:ro -v /data/job/form.json:/job/form.json:ro -v $OUT:/out \
  --entrypoint python3 smh/model-server:latest /opt/smh/train.py > /data/train.log 2>&1
echo "trainer exit $? after $(( $(date +%s) - T0 )) s"; grep -E "TRAINED|Error|error|Traceback" /data/train.log | tail -5; tail -2 /data/train.log | cut -c1-200
ls -la $OUT | grep -v "^total"
python3 - $OUT <<'PY'
import json, sys, os
sys.path.insert(0, "/data/src")
from sealbox import jobform, jobrunner
p = json.load(open("/data/params.json"))
limits = jobrunner.Limits(bounds=jobform.Bounds(max_rank=16, max_steps=2000, allowed_target_modules=tuple(p["params_per_rank"]),
                          allowed_uri_prefixes=("s3://smh-poc-customer-111122223333/",)), params_per_rank=p["params_per_rank"])
form = jobform.validate(json.load(open("/data/src/m4/sample/job-good.json")), limits.bounds)
try:
    jobrunner.check_outputs(sys.argv[1], form, limits); print("OUTPUT CHECK: accepted")
except jobrunner.OutputError as e:
    print("OUTPUT CHECK: REFUSED", e)
size = os.path.getsize(sys.argv[1] + "/adapter_model.safetensors")
print("adapter bytes", size, "| limit", jobrunner.max_adapter_bytes(form, limits), "| base parameters", p["base_params"])
print("metrics", json.load(open(sys.argv[1] + "/metrics.json")) if os.path.exists(sys.argv[1] + "/metrics.json") else None)
PY

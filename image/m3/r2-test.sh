#!/bin/bash
# Risk R2, run on the GPU helper: does unmodified vLLM load a model through
# our decrypting proxy? Also measures host RAM during the load.
set -uo pipefail
cd /data
IMG=smh/model-server:latest; PORT=8765; BR=$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)
PLAIN=/data/out/model.tensors; LOCKED=/data/locked/model.tensors.sbox; KEY=/dev/shm/r2-test.key

echo "== 1. lock the model file with a throwaway test key (8 MiB chunks) =="
mkdir -p /data/locked /data/artifacts
if [ -f "$PLAIN" ]; then
  head -c 32 /dev/urandom > $KEY
  python3 - <<PY
import sys; sys.path.insert(0, "/data/src")
from sealbox import envelope
with open("$PLAIN", "rb") as s, open("$LOCKED", "wb") as d:
    print("chunks:", envelope.encrypt_stream(s, d, open("$KEY", "rb").read(), chunk_size=8 << 20))
PY
  # config and tokenizer files go to their own folder; the plain weights are moved out of reach
  find /data/out -maxdepth 1 -type f ! -name model.tensors -exec cp {} /data/artifacts/ \;
  mkdir -p /data/plain-kept && mv "$PLAIN" /data/plain-kept/model.tensors
fi
ls -la /data/locked /data/artifacts | grep -v "^total"
echo "plain weights size: $(stat -c %s /data/plain-kept/model.tensors)  locked: $(stat -c %s $LOCKED)"

echo "== 2. start the proxy on the container bridge ($BR:$PORT) =="
pkill -f proxy_local.py 2>/dev/null; docker rm -f vllm >/dev/null 2>&1
nohup python3 /data/src/proxy_local.py $LOCKED $KEY $BR $PORT > /data/proxy.log 2>&1 &
sleep 2; cat /data/proxy.log
curl -s -r 0-15 http://$BR:$PORT/model.tensors | od -An -c | head -1

echo "== 3. start unmodified vLLM against the proxy =="
sync; echo 3 > /proc/sys/vm/drop_caches
BASE=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo); echo "host RAM available before: ${BASE} MiB"
T0=$(date +%s)
docker run -d --name vllm --gpus all --ipc=host -e HF_HUB_OFFLINE=1 \
  -v /data/artifacts:/model:ro -p 127.0.0.1:8000:8000 $IMG \
  --model /model --served-model-name demo --load-format tensorizer \
  --model-loader-extra-config "{\"tensorizer_uri\": \"http://$BR:$PORT/model.tensors\"}" \
  ${EXTRA_ARGS:-} >/dev/null
MIN=$BASE; READY=0
for i in $(seq 1 300); do
  AV=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo); [ "$AV" -lt "$MIN" ] && MIN=$AV
  curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && { READY=1; break; }
  [ "$(docker inspect -f '{{.State.Running}}' vllm 2>/dev/null)" = true ] || break
  sleep 1
done
T1=$(date +%s)
echo "ready=$READY after $((T1-T0)) s; peak extra host RAM during load: $((BASE-MIN)) MiB"
if [ "$READY" != 1 ]; then echo "---- vLLM log tail"; docker logs vllm 2>&1 | tail -40; exit 1; fi
docker logs vllm 2>&1 | grep -iE "tensorizer|Deserialized|loading weights took|GiB" | tail -8

echo "== 4. one prompt =="
S=$(date +%s.%N)
R=$(curl -s http://127.0.0.1:8000/v1/completions -H 'Content-Type: application/json' \
  -d '{"model":"demo","prompt":"The capital of France is","max_tokens":64,"temperature":0}')
E=$(date +%s.%N)
echo "$R" | python3 -c "
import sys, json; r = json.load(sys.stdin)
print('answer:', repr(r['choices'][0]['text'][:160])); print('usage:', r['usage'])
print('tokens per second: %.1f' % (r['usage']['completion_tokens'] / ($E - $S)))"
nvidia-smi --query-gpu=memory.used --format=csv,noheader
echo "proxy process RAM: $(ps -o rss= -p $(pgrep -f proxy_local.py | head -1) | awk '{print int($1/1024)}') MiB"
echo "R2 DONE"

#!/bin/bash
# GPU helper, phase 3: vLLM with the base model through our proxy, adapters
# enabled, as an unprivileged user, on half the GPU. Load the trained adapter
# and compare answers. Then train again while it serves.
set -uo pipefail
cd /data
IMG=smh/model-server:latest; PORT=8765; BR=$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)
KEY=/dev/shm/test.key; mkdir -p /data/locked /data/artifacts /data/plain-kept
if [ -f /data/out/model.tensors ]; then
  head -c 32 /dev/urandom > $KEY
  python3 -c "
import sys; sys.path.insert(0,'/data/src')
from sealbox import envelope
with open('/data/out/model.tensors','rb') as s, open('/data/locked/model.tensors.sbox','wb') as d: envelope.encrypt_stream(s, d, open('$KEY','rb').read(), chunk_size=8<<20)"
  find /data/out -maxdepth 1 -type f ! -name model.tensors -exec cp {} /data/artifacts/ \;
  mv /data/out/model.tensors /data/plain-kept/model.tensors
fi
pkill -f proxy_local.py 2>/dev/null; docker rm -f vllm >/dev/null 2>&1
nohup python3 /data/src/proxy_local.py /data/locked/model.tensors.sbox $KEY $BR $PORT > /data/proxy.log 2>&1 &
sleep 2
docker run -d --name vllm --runtime=nvidia --gpus all --shm-size 2g ${VLLM_USER:---user 1000:1000 -e HOME=/tmp} \
  --read-only --tmpfs /tmp:exec --tmpfs /root:exec --cap-drop ALL --security-opt no-new-privileges \
  -e HF_HUB_OFFLINE=1 -e VLLM_NO_USAGE_STATS=1 -e DO_NOT_TRACK=1 -e VLLM_ALLOW_RUNTIME_LORA_UPDATING=True \
  -v /data/artifacts:/model:ro -v /data/adapters:/adapters:ro -p 127.0.0.1:8000:8000 $IMG \
  --model /model --served-model-name demo --load-format tensorizer \
  --model-loader-extra-config "{\"tensorizer_uri\": \"http://$BR:$PORT/model.tensors\"}" \
  --max-logprobs 0 --enable-lora --max-lora-rank 16 --max-loras 4 --gpu-memory-utilization 0.45 >/dev/null
READY=0; for i in $(seq 1 240); do curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && { READY=1; break; }; [ "$(docker inspect -f '{{.State.Running}}' vllm 2>/dev/null)" = true ] || break; sleep 1; done
echo "ready=$READY uid=$(docker exec vllm id -u 2>/dev/null) gpu_used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
[ "$READY" = 1 ] || { docker logs vllm 2>&1 | grep -E "Error|error|denied|Permission" | tail -8 | cut -c1-220; exit 1; }
ask() { curl -s http://127.0.0.1:8000/v1/completions -H 'Content-Type: application/json' -d "{\"model\":\"$1\",\"prompt\":\"Question: Who is Zebby?\\nAnswer:\",\"max_tokens\":40,\"temperature\":0}" | python3 -c "import sys,json; r=json.load(sys.stdin); print(repr(r['choices'][0]['text'][:150]) if 'choices' in r else str(r)[:200])"; }
echo "base model     : $(ask demo)"
echo "load adapter   : $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/load_lora_adapter -H 'Content-Type: application/json' -d '{"lora_name":"zebby-1","lora_path":"/adapters/zebby-1"}')"
echo "with adapter   : $(ask zebby-1)"
echo "base again     : $(ask demo)"
echo "== train a second adapter while vLLM serves =="
( for i in $(seq 1 60); do curl -s -m 5 http://127.0.0.1:8000/v1/completions -H 'Content-Type: application/json' -d '{"model":"demo","prompt":"Hi","max_tokens":4}' -o /dev/null -w "%{http_code}\n"; sleep 1; done > /data/during.txt ) &
bash /data/src/m4/helper-2-train.sh zebby-2 2>&1 | grep -E "trainer exit|OUTPUT CHECK"
wait
echo "prompts answered during training: $(grep -c 200 /data/during.txt) of $(wc -l < /data/during.txt); gpu_used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
echo PHASE3 DONE

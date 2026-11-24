#!/bin/bash
# GPU helper, memory question (Q3): load a much larger model through the same
# proxy on the same 16 GiB machine and measure host RAM. Uses the local NVMe disk.
set -uo pipefail
MODEL=${1:-Qwen/Qwen2.5-7B-Instruct}; D=/data2; IMG=smh/model-server:latest; PORT=8766
if ! mountpoint -q $D; then mkfs.xfs -q -f /dev/nvme1n1 && mkdir -p $D && mount /dev/nvme1n1 $D; fi
mkdir -p $D/hf $D/out $D/locked $D/artifacts
docker rm -f vllm >/dev/null 2>&1; pkill -f "proxy_local.py $D" 2>/dev/null
docker run --rm --entrypoint python3 -v $D/hf:/root/.cache/huggingface $IMG -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" >/dev/null 2>&1; echo "download exit $? size $(du -sh $D/hf | cut -f1)"
docker run --rm --gpus all --ipc=host -v $D/hf:/root/.cache/huggingface -v $D/out:/out -v /data/src/tensorize.py:/tensorize.py:ro --entrypoint python3 $IMG /tensorize.py $MODEL > $D/tensorize.log 2>&1; echo "tensorize exit $?"
rm -rf $D/hf
SIZE=$(stat -c %s $D/out/model.tensors); echo "tensorizer file bytes: $SIZE"
head -c 32 /dev/urandom > /dev/shm/q3.key
python3 -c "
import sys; sys.path.insert(0,'/data/src')
from sealbox import envelope
with open('$D/out/model.tensors','rb') as s, open('$D/locked/model.tensors.sbox','wb') as d: print('chunks', envelope.encrypt_stream(s, d, open('/dev/shm/q3.key','rb').read(), chunk_size=8<<20))"
find $D/out -maxdepth 1 -type f ! -name model.tensors -exec cp {} $D/artifacts/ \; ; rm -f $D/out/model.tensors
BR=$(ip -4 -o addr show docker0 | awk '{print $4}' | cut -d/ -f1)
( python3 /data/src/proxy_local.py $D/locked/model.tensors.sbox /dev/shm/q3.key $BR $PORT > $D/proxy.log 2>&1 & echo $! > $D/proxy.pid )
sleep 2; sync; echo 3 > /proc/sys/vm/drop_caches
BASE=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo); MIN=$BASE; T0=$(date +%s)
docker run -d --name vllm --runtime=nvidia --gpus all --shm-size 2g --user 1000:1000 -e HOME=/tmp \
  --read-only --tmpfs /tmp:exec --tmpfs /root:exec --cap-drop ALL --security-opt no-new-privileges \
  -e HF_HUB_OFFLINE=1 -e VLLM_NO_USAGE_STATS=1 -e DO_NOT_TRACK=1 -v $D/artifacts:/model:ro -p 127.0.0.1:8000:8000 $IMG \
  --model /model --served-model-name demo --load-format tensorizer --max-model-len 4096 \
  --model-loader-extra-config "{\"tensorizer_uri\": \"http://$BR:$PORT/model.tensors\"}" >/dev/null
READY=0; for i in $(seq 1 600); do AV=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo); [ "$AV" -lt "$MIN" ] && MIN=$AV; curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1 && { READY=1; break; }; [ "$(docker inspect -f '{{.State.Running}}' vllm 2>/dev/null)" = true ] || break; sleep 1; done
echo "Q3 RESULT model=$MODEL file_mib=$((SIZE/1048576)) ready=$READY seconds=$(( $(date +%s) - T0 )) ram_before_mib=$BASE ram_lowest_mib=$MIN peak_extra_mib=$((BASE-MIN)) proxy_rss_mib=$(ps -o rss= -p $(cat $D/proxy.pid) | awk '{print int($1/1024)}')"
docker logs vllm 2>&1 | grep -E "Memory usage (before|after)|Model loading took" | cut -c1-230
[ "$READY" = 1 ] && curl -s http://127.0.0.1:8000/v1/completions -H 'Content-Type: application/json' -d '{"model":"demo","prompt":"The capital of France is","max_tokens":16,"temperature":0}' | python3 -c "import sys,json; print('answer', repr(json.load(sys.stdin)['choices'][0]['text'][:80]))"
[ "$READY" = 1 ] || docker logs vllm 2>&1 | grep -E "Error|error|OOM|Killed" | tail -5 | cut -c1-200
docker rm -f vllm >/dev/null 2>&1; kill $(cat $D/proxy.pid) 2>/dev/null; rm -f /dev/shm/q3.key
echo Q3 DONE

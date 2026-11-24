#!/bin/bash
# GPU helper, phase 1: image with the trainer, model download, tensorize.
set -x
MODEL=Qwen/Qwen2.5-1.5B-Instruct; BASEIMG=vllm/vllm-openai@sha256:20fdb089454afbb324efa4110a2c025ff7b27cc1a086e07aa580b18df8ad1ce1
mkdir -p /data/hf /data/out /data/build /data/base /data/adapters /data/job
docker pull -q $BASEIMG
# find the newest peft that installs cleanly next to this vLLM, then pin it
PEFT=$(docker run --rm --entrypoint bash $BASEIMG -c "pip install --dry-run peft 2>/dev/null | grep -o 'peft-[0-9.]*' | tail -1 | cut -d- -f2")
echo "PEFT VERSION ${PEFT}"; echo "$PEFT" > /data/peft-version.txt
cp /data/src/m4/Dockerfile /data/src/m4/train.py /data/build/
( cd /data/build && docker build -q --build-arg PEFT_VERSION=$PEFT -t smh/model-server:latest . )
docker run --rm --entrypoint python3 smh/model-server:latest -c "import vllm, tensorizer, peft, transformers, torch; print('VERSIONS vllm', vllm.__version__, 'tensorizer', tensorizer.__version__, 'peft', peft.__version__, 'transformers', transformers.__version__, 'torch', torch.__version__)"
docker run --rm --entrypoint python3 -v /data/hf:/root/.cache/huggingface smh/model-server:latest -c "from huggingface_hub import snapshot_download; print('MODEL AT', snapshot_download('$MODEL'))"
SNAP=$(ls -d /data/hf/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/*/ | head -1)
cp -rL "$SNAP". /data/base/ && chmod -R a+rX /data/base && ls -la /data/base | head -12
docker run --rm --gpus all --ipc=host -v /data/hf:/root/.cache/huggingface -v /data/out:/out -v /data/src/tensorize.py:/tensorize.py:ro --entrypoint python3 smh/model-server:latest /tensorize.py $MODEL > /data/tensorize.log 2>&1; echo "tensorize exit $?"
ls -la /data/out | grep -v "^total"
echo PHASE1 DONE

#!/bin/bash
# Design steps 1 and 2 as ONE fixed step, run on the GPU helper as root:
# build the model-server container, fetch the open model, save it in the
# loading format, count the adapter limits, then lock everything under a
# fresh data key and upload it to s3://<bucket>/<PREFIX>/.
#   PREFIX=m7 MODEL=Qwen/Qwen2.5-1.5B-Instruct bash lock-artifacts.sh
# (In M3 and M4 this was five hand-run scripts.)
set -euo pipefail
: "${PREFIX:?bucket folder}" "${MODEL:?model name on Hugging Face}"
SRC=/opt/smh/src; D=/data
BASEIMG=$(grep -m1 '^FROM ' "$SRC/image/m4/Dockerfile" | cut -d' ' -f2)
PEFT=$(grep -m1 'ARG PEFT_VERSION=' "$SRC/image/m4/Dockerfile" | cut -d= -f2)
rm -rf $D/out $D/base $D/build $D/artifacts $D/plain-kept $D/upload
mkdir -p $D/hf $D/out $D/base $D/build $D/artifacts $D/plain-kept

echo "== 1. container: $BASEIMG plus tensorizer and peft $PEFT =="
docker pull -q "$BASEIMG" >/dev/null
cp "$SRC/image/m4/Dockerfile" "$SRC/image/m4/train.py" $D/build/
( cd $D/build && docker build -q --build-arg PEFT_VERSION="$PEFT" -t smh/model-server:latest . >/dev/null )
echo "$PEFT" > $D/peft-version.txt

echo "== 2. model $MODEL =="
BUCKET=smh-poc-artifacts-111122223333; REGION=us-east-1; OWN=""; MODELARG="$MODEL"; MOUNT=""
if [[ "$MODEL" == local/* ]]; then
  # The builder's OWN weights (from their disk, a network share or their own store), staged by `smh.sh ship <folder>`.
  OWN="deploy/models-in/${MODEL#local/}"; rm -rf $D/own; mkdir -p $D/own
  aws s3 sync --only-show-errors --region $REGION --exclude model-info.json "s3://$BUCKET/$OWN/" $D/own/
  [ -s $D/own/config.json ] || { echo "MODEL PROBLEM: the model folder has no config.json (it must be a Hugging Face style folder)"; exit 1; }
  ls $D/own/*.safetensors >/dev/null 2>&1 || { echo "MODEL PROBLEM: the model folder has no .safetensors weight files"; exit 1; }
  cp -rL $D/own/. $D/base/ && chmod -R a+rX $D/base
  # vLLM loads a model from a folder path without trouble, but its tensorize step ends by asking Hugging Face for
  # the model's small files by name (Read in code: serialize_extra_artifacts). image/m3/own/sitecustomize.py hands
  # those files over from the folder instead. It is loaded by every Python process in this one container run.
  chmod -R a+rX $D/own
  MODELARG=/own; MOUNT="-v $D/own:/own:ro -v $SRC/image/m3/own:/smh-own:ro -e PYTHONPATH=/smh-own"
else
  docker run --rm --entrypoint python3 -v $D/hf:/root/.cache/huggingface smh/model-server:latest \
    -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" > $D/download.log 2>&1 \
    || { echo "MODEL PROBLEM: the model could not be downloaded from Hugging Face:"; tail -4 $D/download.log | cut -c1-400; exit 1; }
  SNAP=$(ls -d $D/hf/hub/models--${MODEL//\//--}/snapshots/*/ | head -1)
  cp -rL "$SNAP". $D/base/ && chmod -R a+rX $D/base
fi
python3 "$SRC/image/m4/params.py" $D/base > $D/params.json

echo "== 3. loading format (needs the GPU) =="
docker run --rm --gpus all --ipc=host $MOUNT -v $D/hf:/root/.cache/huggingface -v $D/out:/out \
  -v "$SRC/image/m3/tensorize.py":/tensorize.py:ro --entrypoint python3 smh/model-server:latest /tensorize.py "$MODELARG" > $D/tensorize.log 2>&1 \
  || { echo "MODEL PROBLEM: the model could not be put in the loading format (vLLM could not load it):"; grep -E "Error|error:|Exception|File \"/usr/local/lib/python3.12/dist-packages/(vllm|huggingface_hub)" $D/tensorize.log | tail -14 | cut -c1-400; exit 1; }
find $D/out -maxdepth 1 -type f ! -name model.tensors -exec cp {} $D/artifacts/ \;
mv $D/out/model.tensors $D/plain-kept/model.tensors
[ -s $D/artifacts/config.json ] || { echo "no config file next to the weights"; exit 1; }

echo "== 4. lock and upload to $PREFIX/ =="
PREFIX="$PREFIX" MODEL="$MODEL" python3 "$SRC/image/pipeline/lock-and-upload.py" | grep -E "chunks|uploaded|DONE"
# The plain copies are no longer needed on this machine.
rm -rf $D/plain-kept $D/base $D/out $D/upload $D/own
# The builder's staged plain weights have done their job: the bucket goes back to holding ciphertext only.
# The small description (file names and sizes, no weights) stays, so a later look at this release still finds the model.
if [ -n "$OWN" ]; then aws s3 rm --only-show-errors --region $REGION --recursive --exclude model-info.json "s3://$BUCKET/$OWN/"; echo "staged plain weights removed"; fi
echo "LOCK OK"

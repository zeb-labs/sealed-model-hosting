# Our model server and trainer, one image: the official vLLM image pinned by
# digest, plus tensorizer (loading format) and peft (LoRA training), both
# pinned, plus the builder's trainer script. Nothing else.
FROM vllm/vllm-openai@sha256:20fdb089454afbb324efa4110a2c025ff7b27cc1a086e07aa580b18df8ad1ce1
ARG PEFT_VERSION=0.22.0
RUN pip install --no-cache-dir "tensorizer==2.12.1" "peft==${PEFT_VERSION}" || uv pip install --system --no-cache "tensorizer==2.12.1" "peft==${PEFT_VERSION}"
COPY train.py /opt/smh/train.py

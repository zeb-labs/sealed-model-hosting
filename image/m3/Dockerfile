# Our model server: the official vLLM image, pinned by digest, plus tensorizer
# (the loading format), pinned to the version we reviewed. Nothing else.
FROM vllm/vllm-openai@sha256:20fdb089454afbb324efa4110a2c025ff7b27cc1a086e07aa580b18df8ad1ce1
RUN pip install --no-cache-dir "tensorizer==2.12.1" || uv pip install --system --no-cache "tensorizer==2.12.1"

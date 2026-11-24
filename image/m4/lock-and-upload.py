"""Run on the GPU helper. Locks the M4 artifacts under ONE fresh KMS data
key (separate derived keys per purpose) and uploads them. The plain data key
exists only in this process's memory.

  m4/model-hf.sbox        the training copy of the weights, standard format (a tar), 8 MiB chunks
  m4/wrapped.key          the data key, wrapped by the model key (only KMS can unwrap)
  m4/model.tensors.sbox   the weights (tensorizer format inside), 8 MiB chunks
  m4/model-config.sbox    config and tokenizer files (a tar)
  m4/model-server.sbox    our model-server container image (docker save | zstd)
"""
import base64, hashlib, io, json, subprocess, sys, tarfile, time

sys.path.insert(0, "/data/src")
from sealbox import envelope

REGION, BUCKET, PREFIX = "us-east-1", "smh-poc-artifacts-111122223333", "m4"
OUT = "/data/upload"
subprocess.run(["mkdir", "-p", OUT], check=True)

d = json.loads(subprocess.run(
    ["aws", "kms", "generate-data-key", "--region", REGION, "--key-id", "alias/smh-poc-model-key",
     "--key-spec", "AES_256", "--output", "json"], check=True, capture_output=True).stdout)
open(f"{OUT}/wrapped.key", "wb").write(base64.b64decode(d["CiphertextBlob"]))
key = base64.b64decode(d["Plaintext"]); del d
k_weights, k_containers = envelope.derive_key(key, b"weights"), envelope.derive_key(key, b"containers")
del key

t = time.time()
with open("/data/plain-kept/model.tensors", "rb") as s, open(f"{OUT}/model.tensors.sbox", "wb") as o:
    print("weights chunks:", envelope.encrypt_stream(s, o, k_weights, chunk_size=8 << 20), "in %.0f s" % (time.time() - t), flush=True)

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as tar:
    tar.add("/data/artifacts", arcname=".")
buf.seek(0)
with open(f"{OUT}/model-config.sbox", "wb") as o:
    print("config chunks:", envelope.encrypt_stream(buf, o, k_weights), flush=True)

t = time.time()
tar = subprocess.Popen(["tar", "-c", "-C", "/data/base", "."], stdout=subprocess.PIPE)
with open(f"{OUT}/model-hf.sbox", "wb") as o:
    print("training copy chunks:", envelope.encrypt_stream(tar.stdout, o, k_weights, chunk_size=8 << 20), "in %.0f s" % (time.time() - t), flush=True)
assert tar.wait() == 0

t = time.time()
save = subprocess.Popen("docker save smh/model-server:latest | zstd -T0 -3 -c", shell=True, stdout=subprocess.PIPE)
with open(f"{OUT}/model-server.sbox", "wb") as o:
    print("container chunks:", envelope.encrypt_stream(save.stdout, o, k_containers), "in %.0f s" % (time.time() - t), flush=True)
assert save.wait() == 0
del k_weights, k_containers

h = hashlib.sha256()
with open(f"{OUT}/model-server.sbox", "rb") as f:
    for block in iter(lambda: f.read(1 << 20), b""):
        h.update(block)
summary = {"model": "Qwen/Qwen2.5-1.5B-Instruct",
           "base_image": "vllm/vllm-openai@sha256:20fdb089454afbb324efa4110a2c025ff7b27cc1a086e07aa580b18df8ad1ce1",
           "vllm": "0.34.0", "tensorizer": "2.12.1", "peft": open("/data/peft-version.txt").read().strip(),
           **json.load(open("/data/params.json")),
           "images": [{"name": "model-server", "digest": "sha256:" + h.hexdigest()}]}
json.dump(summary, open(f"{OUT}/m4-artifacts.json", "w"), indent=1)
print(json.dumps(summary, indent=1), flush=True)
for name in ("wrapped.key", "model.tensors.sbox", "model-config.sbox", "model-hf.sbox", "model-server.sbox", "m4-artifacts.json"):
    subprocess.run(["aws", "s3", "cp", "--only-show-errors", "--region", REGION, f"{OUT}/{name}", f"s3://{BUCKET}/{PREFIX}/{name}"], check=True)
    print("uploaded", name, flush=True)
print("LOCK AND UPLOAD DONE")

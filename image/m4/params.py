"""Count LoRA parameters per unit of rank for each attention projection, from
the base model's safetensors headers (no GPU, no model load). The result goes
into the job runner's limits: it fixes how big an adapter may be."""
import glob, json, struct, sys

per_rank, total = {}, 0
for path in sorted(glob.glob(sys.argv[1] + "/*.safetensors")):
    with open(path, "rb") as f:
        (n,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(n))
    for name, info in header.items():
        if name == "__metadata__":
            continue
        size = 1
        for d in info["shape"]:
            size *= d
        total += size
        for m in ("q_proj", "k_proj", "v_proj", "o_proj"):
            if name.endswith(f".{m}.weight"):
                per_rank[m] = per_rank.get(m, 0) + info["shape"][0] + info["shape"][1]
print(json.dumps({"params_per_rank": per_rank, "base_params": total}))

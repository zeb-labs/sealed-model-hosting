"""The sealed trainer. Runs inside the model-server image, in a container with
no network. The builder writes this code; the customer supplies only data and
the few numbers in the checked form.

  /base            base model in standard format, read-only
  /job/form.json   rank, alpha, learning_rate, max_steps, target_modules, seed
  /job/data.jsonl  one example per line: {"prompt": "...", "completion": "..."}
  /out             receives exactly: adapter_model.safetensors, adapter_config.json, metrics.json

Fixed on purpose: optimiser, batch size, sequence length, and what is saved.
Only the adapter is trained. The base weights are never written anywhere.
"""
import json
import os
import shutil
import tempfile

import torch
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

BATCH, MAX_TOKENS, MAX_EXAMPLES = 4, 256, 5000

form = json.load(open("/job/form.json"))
torch.manual_seed(int(form["seed"]))

examples = []
with open("/job/data.jsonl", encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        row = json.loads(line)
        if not (isinstance(row, dict) and isinstance(row.get("prompt"), str) and isinstance(row.get("completion"), str)):
            raise SystemExit("each data line must be an object with text fields prompt and completion")
        examples.append((row["prompt"], row["completion"]))
        if len(examples) > MAX_EXAMPLES:
            raise SystemExit("too many examples")
if not examples:
    raise SystemExit("the data file has no examples")

tok = AutoTokenizer.from_pretrained("/base")
if tok.pad_token is None:
    tok.pad_token = tok.eos_token
model = AutoModelForCausalLM.from_pretrained("/base", torch_dtype=torch.bfloat16).to("cuda")
model = get_peft_model(model, LoraConfig(
    r=int(form["rank"]), lora_alpha=int(form["alpha"]), target_modules=list(form["target_modules"]),
    lora_dropout=0.0, bias="none", task_type="CAUSAL_LM"))
optim = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=float(form["learning_rate"]))


def batch(i):
    rows = [examples[(i * BATCH + k) % len(examples)] for k in range(BATCH)]
    ids, labels = [], []
    for prompt, completion in rows:
        p = tok(prompt, add_special_tokens=False)["input_ids"]
        c = tok(completion, add_special_tokens=False)["input_ids"] + [tok.eos_token_id]
        x = (p + c)[:MAX_TOKENS]
        ids.append(x)
        labels.append(([-100] * len(p) + c)[:MAX_TOKENS])  # learn the completion only
    width = max(len(x) for x in ids)
    pad = lambda rows, v: torch.tensor([r + [v] * (width - len(r)) for r in rows], device="cuda")
    x = pad(ids, tok.pad_token_id)
    return x, pad([[1] * len(r) for r in ids], 0), pad(labels, -100)


model.train()
steps, curve = int(form["max_steps"]), []
every = max(1, steps // 100)
for step in range(steps):
    x, mask, y = batch(step)
    loss = model(input_ids=x, attention_mask=mask, labels=y).loss
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
    optim.step()
    optim.zero_grad(set_to_none=True)
    if step % every == 0 or step == steps - 1:
        curve.append(round(float(loss), 4))

# Save to a scratch folder first and copy out only the three allowed files.
with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
    model.save_pretrained(tmp, safe_serialization=True)
    for name in ("adapter_model.safetensors", "adapter_config.json"):
        shutil.copyfile(os.path.join(tmp, name), os.path.join("/out", name))
json.dump({"steps": steps, "final_loss": curve[-1], "loss_curve": curve, "examples": len(examples)},
          open("/out/metrics.json", "w"))
print("TRAINED", steps, "steps, final loss", curve[-1])

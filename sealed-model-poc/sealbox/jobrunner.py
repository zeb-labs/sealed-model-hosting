"""The job runner: fine-tuning on the sealed machine, with no way in.

The customer puts a job form (JSON) and a data file into their OWN bucket. This
service looks there, and for each new job:

1. Checks the form with `jobform` (known fields, builder's limits, locations
   inside the customer's bucket only). A refused form never reaches training.
2. Fetches the data and runs the trainer: the builder's code, in a container
   with no network, no credentials and no TPM.
3. Checks what the trainer produced. Only a LoRA adapter of the expected shape
   and size, its small config, and a short metrics file may leave.
4. Stores them in the customer's bucket under the customer's key, tells the
   model server to load the adapter, and writes a status file.

Everything that touches the outside world goes through `Ops`, so the rules
can be tested offline with fakes (tests/test_jobrunner.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import jobform, meter

_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_LORA_TENSOR = re.compile(r"\.lora_[AB](\.[A-Za-z0-9_]+)?\.weight$")
ADAPTER, ADAPTER_CONFIG, METRICS = "adapter_model.safetensors", "adapter_config.json", "metrics.json"
_METRIC_KEYS = {"steps", "final_loss", "loss_curve", "examples"}


class OutputError(Exception):
    """The trainer produced something that is not allowed to leave."""


@dataclass
class Limits:
    """Set by the builder in the recipe, baked into the measured image."""
    bounds: jobform.Bounds
    # LoRA parameters added per unit of rank, for each target module name
    # (layers x (inputs + outputs)). Fixes how big an adapter may be.
    params_per_rank: Dict[str, int]
    max_data_bytes: int = 50 << 20
    jobs_per_day: int = 5
    max_curve_points: int = 200


@dataclass
class Ops:
    list_new_jobs: Callable[[], List[str]]            # job ids with a form and no status yet
    read_form: Callable[[str], Any]                   # parsed JSON of jobs/<id>.json
    fetch_data: Callable[[str, int], str]             # (uri, max bytes) -> local path
    train: Callable[[jobform.JobForm, str, str], None]  # (form, data path, output folder)
    store: Callable[[str, str], None]                 # (local path, s3 uri), under the customer's key
    load_adapter: Callable[[str, str], None]          # (job id, output folder)
    write_status: Callable[[str, Dict[str, Any]], None]
    new_output_dir: Callable[[str], str]
    jobs_today: Callable[[], int]
    say: Callable[[str], None]
    # Optional. Ask the model one question from the customer's own data, with
    # and without the new adapter. The answers go only into the customer's status file.
    self_check: Optional[Callable[[str, str], Dict[str, str]]] = None


def max_adapter_bytes(form: jobform.JobForm, limits: Limits) -> int:
    params = sum(limits.params_per_rank[m] for m in form.target_modules) * form.rank
    return params * 4 + (1 << 16)  # float32 at most, plus the file header


def _safetensors_header(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        raw = f.read(8)
        if len(raw) != 8:
            raise OutputError("adapter file is too short")
        (size,) = struct.unpack("<Q", raw)
        if not 2 <= size <= 1 << 20:
            raise OutputError("adapter file header has an impossible size")
        try:
            header = json.loads(f.read(size))
        except (ValueError, RecursionError):
            raise OutputError("adapter file header is not valid") from None
        if not isinstance(header, dict):
            raise OutputError("adapter file header is not valid")
        return header


def check_outputs(out_dir: str, form: jobform.JobForm, limits: Limits) -> None:
    """Raise OutputError unless `out_dir` holds exactly an allowed LoRA result."""
    names = sorted(os.listdir(out_dir))
    if names != sorted([ADAPTER, ADAPTER_CONFIG, METRICS]):
        raise OutputError("unexpected files in the output: " + ", ".join(names)[:200])
    for name in names:
        path = os.path.join(out_dir, name)
        if os.path.islink(path) or not os.path.isfile(path):
            raise OutputError(f"{name} is not a plain file")

    # 1. The adapter: bounded size, and only LoRA tensors of the requested rank.
    path = os.path.join(out_dir, ADAPTER)
    if os.path.getsize(path) > max_adapter_bytes(form, limits):
        raise OutputError("adapter is larger than this form allows")
    header = _safetensors_header(path)
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    if not tensors:
        raise OutputError("adapter holds no tensors")
    for tensor, info in tensors.items():
        if not _LORA_TENSOR.search(tensor) or not any(f".{m}." in tensor for m in form.target_modules):
            raise OutputError(f"adapter holds a tensor that is not a requested LoRA weight: {tensor[:80]}")
        shape = info.get("shape") if isinstance(info, dict) else None
        if not (isinstance(shape, list) and len(shape) == 2 and form.rank in shape):
            raise OutputError(f"a tensor does not have the requested rank: {tensor[:80]}")

    # 2. The adapter's config: small, and it says what the form said.
    path = os.path.join(out_dir, ADAPTER_CONFIG)
    if os.path.getsize(path) > 16 << 10:
        raise OutputError("adapter config is too large")
    try:
        cfg = json.load(open(path))
    except (ValueError, RecursionError):
        raise OutputError("adapter config is not valid JSON") from None
    modules = cfg.get("target_modules") if isinstance(cfg, dict) else None
    if not (isinstance(modules, list) and all(isinstance(m, str) for m in modules)):
        raise OutputError("adapter config does not match the form")
    if cfg.get("r") != form.rank or sorted(modules) != sorted(form.target_modules):
        raise OutputError("adapter config does not match the form")

    # 3. Metrics: a few numbers only, so they cannot carry anything else out.
    path = os.path.join(out_dir, METRICS)
    if os.path.getsize(path) > 64 << 10:
        raise OutputError("metrics file is too large")
    try:
        metrics = json.load(open(path))
    except (ValueError, RecursionError):
        raise OutputError("metrics file is not valid JSON") from None
    if not isinstance(metrics, dict) or set(metrics) - _METRIC_KEYS:
        raise OutputError("metrics file has fields that are not allowed")
    curve = metrics.get("loss_curve", [])
    numbers = [metrics.get(k, 0) for k in ("steps", "final_loss", "examples")] + (curve if isinstance(curve, list) else [None])
    if len(numbers) - 3 > limits.max_curve_points or any(
        isinstance(n, bool) or not isinstance(n, (int, float)) for n in numbers
    ):
        raise OutputError("metrics must be plain numbers")


def run_once(limits: Limits, ops: Ops) -> None:
    """Handle every new job once. One job at a time."""
    for job_id in sorted(ops.list_new_jobs()):
        if not _JOB_ID.match(job_id):
            ops.say(f"JOB ignored: job name is not allowed ({len(job_id)} characters)")
            continue
        status = _run_job(job_id, limits, ops)
        ops.write_status(job_id, status)
        ops.say(f"JOB {job_id} {status['state']}" + (": " + "; ".join(status["problems"])[:200] if status.get("problems") else ""))


def _run_job(job_id: str, limits: Limits, ops: Ops) -> Dict[str, Any]:
    if ops.jobs_today() >= limits.jobs_per_day:
        return {"state": "refused", "problems": [f"daily limit of {limits.jobs_per_day} jobs reached"]}
    try:
        form = jobform.validate(ops.read_form(job_id), limits.bounds)
    except jobform.JobFormError as error:
        return {"state": "refused", "problems": error.problems}
    except Exception:
        return {"state": "refused", "problems": ["the job form could not be read as JSON"]}
    if not form.output_uri.endswith("/"):
        return {"state": "refused", "problems": ["output_uri must be a folder ending in /"]}

    out_dir = ops.new_output_dir(job_id)
    try:
        data = ops.fetch_data(form.dataset_uri, limits.max_data_bytes)
        ops.train(form, data, out_dir)
        check_outputs(out_dir, form, limits)
    except OutputError as error:
        shutil.rmtree(out_dir, ignore_errors=True)  # a refused result must not stay loadable
        return {"state": "failed", "problems": [f"output refused: {error}"]}
    except Exception as error:
        shutil.rmtree(out_dir, ignore_errors=True)
        # No internals in the customer's status file: only a short, fixed kind of reason.
        return {"state": "failed", "problems": [str(error)[:120] if isinstance(error, (ValueError, RuntimeError)) else "training failed"]}

    for name in (ADAPTER, ADAPTER_CONFIG, METRICS):
        ops.store(os.path.join(out_dir, name), f"{form.output_uri}{job_id}/{name}")
    ops.load_adapter(job_id, out_dir)
    metrics = json.load(open(os.path.join(out_dir, METRICS)))
    status = {"state": "done", "adapter": f"{form.output_uri}{job_id}/", "served_as": job_id,
              "adapter_bytes": os.path.getsize(os.path.join(out_dir, ADAPTER)),
              "final_loss": metrics.get("final_loss"), "steps": metrics.get("steps")}
    if ops.self_check:
        try:
            with open(data, encoding="utf-8") as f:
                prompt = str(json.loads(f.readline()).get("prompt", ""))[:300]
            status["check"] = dict(ops.self_check(job_id, prompt), prompt=prompt)
        except Exception:
            status["check"] = {"error": "self-check could not run"}
    return status


def restore(job_id: str, out_dir: str, limits: Limits) -> None:
    """Re-check an adapter fetched back from the customer's bucket after a restart.

    The customer controls that bucket, so the files are checked again against
    the builder's limits before the model server may load them.
    """
    try:
        cfg = json.load(open(os.path.join(out_dir, ADAPTER_CONFIG)))
        form = jobform.validate(
            {"method": "lora", "rank": cfg.get("r"), "target_modules": cfg.get("target_modules"),
             "dataset_uri": limits.bounds.allowed_uri_prefixes[0] + "restored",
             "output_uri": limits.bounds.allowed_uri_prefixes[0], "learning_rate": limits.bounds.min_learning_rate,
             "max_steps": 1}, limits.bounds)
    except (OSError, ValueError, jobform.JobFormError):
        raise OutputError(f"stored adapter {job_id} is outside the builder's limits") from None
    check_outputs(out_dir, form, limits)


# ---------------------------------------------------------------------------
# The real machine. Thin wrappers; exercised on AWS.
# ---------------------------------------------------------------------------

def _split(uri: str) -> Tuple[str, str]:
    bucket, _, key = uri[len("s3://"):].partition("/")
    return bucket, key


def _real(cfg: Dict[str, Any]) -> Tuple[Limits, Ops]:
    import subprocess
    import time
    import urllib.request

    import boto3

    j = cfg["jobs"]
    s3 = boto3.client("s3", region_name=cfg["region"])
    bucket, work = j["bucket"], j["workdir"]
    limits = Limits(
        bounds=jobform.Bounds(max_rank=j["max_rank"], max_alpha=j["max_alpha"], max_steps=j["max_steps"],
                              min_learning_rate=j["min_learning_rate"], max_learning_rate=j["max_learning_rate"],
                              allowed_target_modules=tuple(j["params_per_rank"]),
                              allowed_uri_prefixes=(f"s3://{bucket}/",)),
        params_per_rank=j["params_per_rank"], max_data_bytes=j["max_data_bytes"], jobs_per_day=j["jobs_per_day"])
    started: List[float] = []

    def say(line: str) -> None:
        print("SMH " + line, flush=True)

    def exists(key: str) -> bool:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def list_new_jobs() -> List[str]:
        out = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="jobs/"):
            for obj in page.get("Contents", []):
                name = obj["Key"][len("jobs/"):]
                if name.endswith(".json") and "/" not in name and obj["Size"] <= 16 << 10:
                    job_id = name[: -len(".json")]
                    if not exists(f"results/{job_id}/status.json"):
                        out.append(job_id)
        return out

    def read_form(job_id: str) -> Any:
        return json.loads(s3.get_object(Bucket=bucket, Key=f"jobs/{job_id}.json")["Body"].read())

    def fetch_data(uri: str, max_bytes: int) -> str:
        b, key = _split(uri)
        if s3.head_object(Bucket=b, Key=key)["ContentLength"] > max_bytes:
            raise ValueError("the data file is larger than the limit")
        path = os.path.join(work, "data.jsonl")
        s3.download_file(b, key, path)
        return path

    def new_output_dir(job_id: str) -> str:
        path = os.path.join(j["adapters_dir"], job_id)
        os.makedirs(path, exist_ok=False)
        os.chmod(path, 0o777)  # the trainer runs as an unprivileged user
        return path

    def train(form: jobform.JobForm, data: str, out_dir: str) -> None:
        started.append(time.time())
        form_path = os.path.join(work, "form.json")
        json.dump({"rank": form.rank, "alpha": form.alpha, "learning_rate": form.learning_rate,
                   "max_steps": form.max_steps, "target_modules": list(form.target_modules), "seed": form.seed},
                  open(form_path, "w"))
        # No network, no credentials, no TPM. Base weights and data are read-only.
        result = subprocess.run(
            ["docker", "run", "--rm", "--name", "trainer", "--network", "none", "--log-driver", "none", "--runtime=nvidia", "--gpus", "all",
             "--read-only", "--tmpfs", "/tmp:exec", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--user", "65534:65534", "-e", "HOME=/tmp", "-e", "HF_HUB_OFFLINE=1",
             "-v", f"{j['base_dir']}:/base:ro", "-v", f"{data}:/job/data.jsonl:ro",
             "-v", f"{form_path}:/job/form.json:ro", "-v", f"{out_dir}:/out",
             "--entrypoint", "python3", j["image"], "/opt/smh/train.py"],
            capture_output=True, timeout=j["train_timeout"])
        if result.returncode != 0:
            # Only the error type: the trainer's own words stay inside.
            last = (result.stderr.decode(errors="replace").strip().splitlines() or ["?"])[-1]
            say("TRAINER failed: " + re.sub(r"[^A-Za-z0-9_.]", "", last.split(":")[0])[:60])
            raise RuntimeError("training failed")

    def store(path: str, uri: str) -> None:
        b, key = _split(uri)
        s3.upload_file(path, b, key, ExtraArgs={"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": j["kms_key"]})

    def load_adapter(job_id: str, out_dir: str) -> None:
        body = json.dumps({"lora_name": job_id, "lora_path": f"/adapters/{job_id}"}).encode()
        req = urllib.request.Request(j["load_url"], data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=120).read()

    def self_check(job_id: str, prompt: str) -> Dict[str, str]:
        out = {}
        for label, model in (("base", j["base_name"]), ("adapter", job_id)):
            body = json.dumps({"model": model, "prompt": prompt, "max_tokens": 48, "temperature": 0}).encode()
            req = urllib.request.Request(j["completions_url"], data=body, headers={"Content-Type": "application/json"})
            out[label] = json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["text"][:300]
            meter.note_own()          # the machine's own request: reported separately in the usage record
        return out

    def restore_all() -> None:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="results/"):
            for obj in page.get("Contents", []):
                parts = obj["Key"].split("/")
                if len(parts) != 3 or parts[2] != "status.json" or not _JOB_ID.match(parts[1]):
                    continue
                job_id = parts[1]
                try:
                    status = json.loads(s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
                    if status.get("state") != "done":
                        continue
                    out_dir = new_output_dir(job_id)
                    b, prefix = _split(status["adapter"])
                    for name in (ADAPTER, ADAPTER_CONFIG, METRICS):
                        if s3.head_object(Bucket=b, Key=prefix + name)["ContentLength"] > 1 << 30:
                            raise OutputError("stored file is too large")
                        s3.download_file(b, prefix + name, os.path.join(out_dir, name))
                    restore(job_id, out_dir, limits)
                    load_adapter(job_id, out_dir)
                    say(f"JOB {job_id} adapter restored from the customer bucket")
                except Exception as error:
                    shutil.rmtree(os.path.join(j["adapters_dir"], job_id), ignore_errors=True)
                    say(f"JOB {job_id} adapter NOT restored: {type(error).__name__}")

    def write_status(job_id: str, status: Dict[str, Any]) -> None:
        s3.put_object(Bucket=bucket, Key=f"results/{job_id}/status.json", Body=json.dumps(status, indent=1).encode(),
                      ServerSideEncryption="aws:kms", SSEKMSKeyId=j["kms_key"])

    def jobs_today() -> int:
        return sum(1 for t in started if time.time() - t < 86400)

    ops = Ops(list_new_jobs, read_form, fetch_data, train, store, load_adapter, write_status,
              new_output_dir, jobs_today, say, self_check)
    ops.restore_all = restore_all  # type: ignore[attr-defined]
    return limits, ops


def main() -> int:
    import time

    from . import loader

    with open("/etc/smh/loader.json") as f:
        cfg = loader.site(json.load(f))
    limits, ops = _real(cfg)
    ops.restore_all()  # type: ignore[attr-defined]
    ops.say(f"JOBS watching s3://{cfg['jobs']['bucket']}/jobs/")
    while True:
        try:
            run_once(limits, ops)
        except Exception as error:
            ops.say(f"JOBS error: {type(error).__name__}")
        time.sleep(cfg["jobs"].get("poll_seconds", 30))


if __name__ == "__main__":
    raise SystemExit(main())

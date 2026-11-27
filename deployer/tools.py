"""The deployer's tools: everything the agent can do, and nothing else.

The agent is not trusted. It may be wrong, or misled by text it reads. So:

- Every input is checked against a pattern or a fixed list. There is no
  input that carries a command, a policy or a measured value.
- Every answer is made of fixed fields: states, IDs, numbers, fingerprints.
  Log text never comes back (a log line could carry instructions).
- A step runs only when the steps before it have finished well.
- `pin_keys` runs only when a human's approval record names exactly this
  build's fingerprint. The agent's roles cannot write that record (IAM).

All AWS calls go through `Cloud`, so the rules can be tested offline
(deployer/test_tools.py). `lambda_handler` is the thin real wiring.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Dict, List, Optional

VERSION = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
PREFIX = re.compile(r"^m[0-9a-z-]{1,20}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,90}$")

# Single-GPU types we allow, with GPU memory in GiB. Order = order of preference.
MACHINES = [("g5.xlarge", 24), ("g6.xlarge", 24), ("g5.2xlarge", 24), ("g6e.xlarge", 48)]
# M3 finding (Run): loading needs about twice the model's size in GPU memory.
LOAD_FACTOR, HEADROOM_GIB = 2.0, 2.0

# step -> (machine it runs on, steps that must have finished well first)
STEPS: Dict[str, tuple] = {
    "lock": ("helper", ()),
    "build": ("builder", ("lock",)),
    "register": ("builder", ("build",)),
    "check": ("builder", ("register",)),
    "package": ("builder", ("register",)),
    "pin": ("builder", ("register", "check")),     # only through pin_keys
}
# Fields a step's result file may contribute to an answer. Anything else is dropped.
RESULT_FIELDS = ("ok", "exit", "ami", "snapshot", "pcr4", "pcr7", "pcr12", "pcr15_open", "pcr15_locked", "packages",
                 "image_check", "summary_sha256", "checks", "sizes", "stacks",
                 "ready", "keys", "kernel", "driver", "lease_secret", "disk_free_gib")
# First-time setup is not a release: its state lives under this fixed name.
SETUP = "0.0.0"
# The fixed pieces of the builder's side, in the order they depend on each other. The person's own command
# (bash smh.sh setup) creates them; the agent can see them, and it prepares the build machine itself.
PIECES = ("smh-poc-cost-guard", "smh-poc-m1-base", "smh-poc-m1-key", "smh-poc-m1-lease", "smh-poc-m1-builder")
MUST_PASS_BEFORE_APPROVAL = ("S4", "S5", "S6", "S7", "S8", "S9")


class Refused(Exception):
    """The tool will not do this. The message is safe to show the agent."""


class Cloud:
    """What the tools need from AWS. The Lambda supplies the real one; tests supply a fake."""

    get: Callable[[str], Optional[bytes]]                 # object in the builder's bucket, or None
    put: Callable[[str, bytes], None]
    send_step: Callable[..., str]                         # (machine, step, version, prefix, model, gpu) -> command id
    command_state: Callable[[str, str], str]              # (machine, command id) -> running | done | failed
    machine_state: Callable[[str], str]                   # builder | helper -> absent, starting, online, stopped, failed:<reason>
    machine_up: Callable[[str, str], None]                # (machine, gpu type)
    machine_down: Callable[[str], None]
    model_info: Callable[[str], Dict[str, Any]]           # Hugging Face's public description of a model
    stack_state: Callable[[str], str]                     # one of PIECES -> ready | absent | <its CloudFormation status>


def _check(value: Any, pattern: "re.Pattern[str]", what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise Refused(f"{what} is not in the allowed form")
    return value


def _json(cloud: Cloud, key: str) -> Optional[Dict[str, Any]]:
    raw = cloud.get(key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _state(cloud: Cloud, version: str) -> Dict[str, Any]:
    state = _json(cloud, f"deploy/{_check(version, VERSION, 'version')}/state.json")
    if state is None:
        raise Refused("no deployment with this version; call start_deployment first")
    return state


def _result(cloud: Cloud, version: str, step: str) -> Optional[Dict[str, Any]]:
    result = _json(cloud, f"deploy/{version}/{step}.json")
    if result is None:
        return None
    return {k: result[k] for k in RESULT_FIELDS if k in result}


def _step_state(cloud: Cloud, state: Dict[str, Any], version: str, step: str) -> Dict[str, Any]:
    command = state.get("commands", {}).get(step)
    if not command:
        return {"step": step, "state": "not_started"}
    if cloud.command_state(command["machine"], command["id"]) == "running":
        return {"step": step, "state": "running"}
    # The step's own result file decides. (The machine it ran on may be gone by
    # now: the helper is deleted after the lock step.) Starting a step first
    # writes a "not finished" result, so an old good result cannot be mistaken
    # for a new run's.
    result = _result(cloud, version, step)
    if result and result.get("ok") is True:
        return {"step": step, "state": "done", **result}
    # No free text: the human reads the full log, the agent gets the fact.
    return {"step": step, "state": "failed", "exit": (result or {}).get("exit"),
            "note": "the full log is in the bucket under deploy/<version>/logs/ for a human to read"}


# ---------------------------------------------------------------- the tools

def profile_model(cloud: Cloud, model: str) -> Dict[str, Any]:
    info = cloud.model_info(_check(model, MODEL, "model name"))
    weights = sum(int(f.get("size") or 0) for f in info.get("siblings", []) if str(f.get("rfilename", "")).endswith(".safetensors"))
    parameters = int((info.get("safetensors") or {}).get("total") or 0)
    if weights <= 0:
        raise Refused("this model has no safetensors weight files")
    return {"model": model, "weights_bytes": weights, "parameters": parameters}


def choose_machine(cloud: Cloud, weights_bytes: int) -> Dict[str, Any]:
    if type(weights_bytes) is not int or not 0 < weights_bytes < 1 << 40:
        raise Refused("weights_bytes is not a plausible size")
    need = weights_bytes / (1 << 30) * LOAD_FACTOR + HEADROOM_GIB
    fits = [name for name, gib in MACHINES if gib >= need]
    if not fits:
        raise Refused(f"no single-GPU machine in the table has the {need:.0f} GiB this model needs to load")
    return {"gpu_memory_needed_gib": round(need, 1), "choices_in_order": fits,
            "rule": "loading needs about twice the model's size in GPU memory (measured in M3)"}


def start_deployment(cloud: Cloud, version: str, model: str, prefix: str, gpu: str) -> Dict[str, Any]:
    _check(version, VERSION, "version"); _check(model, MODEL, "model name"); _check(prefix, PREFIX, "folder")
    if version == SETUP:
        raise Refused("version 0.0.0 is kept for first-time setup; use another")
    if gpu not in [name for name, _ in MACHINES]:
        raise Refused("machine type is not in the table")
    wanted = {"version": version, "model": model, "prefix": prefix, "gpu": gpu}
    existing = _json(cloud, f"deploy/{version}/state.json")
    if existing is not None:
        if {k: existing.get(k) for k in ("version", "model", "prefix")} != {k: wanted[k] for k in ("version", "model", "prefix")}:
            raise Refused("this version already exists with another model or folder")
        return {**{k: existing[k] for k in wanted}, "resumed": True}
    if cloud.get(f"{prefix}/artifacts.json") is not None:     # the one object in a model folder the tools may read
        raise Refused("the folder is not clean: it already holds locked objects")
    cloud.put(f"deploy/{version}/state.json", json.dumps({**wanted, "commands": {}}).encode())
    return {**wanted, "resumed": False}


def machine(cloud: Cloud, version: str, which: str, action: str, gpu: Optional[str] = None) -> Dict[str, Any]:
    state = _state(cloud, version)
    if which not in ("builder", "helper") or action not in ("up", "down"):
        raise Refused("machine must be builder or helper; action must be up or down")
    if action == "down":
        for step, command in state.get("commands", {}).items():
            if command["machine"] == which and cloud.command_state(which, command["id"]) == "running":
                raise Refused(f"step {step} is still running on the {which}")
        cloud.machine_down(which)
    else:
        if gpu is not None and gpu not in [name for name, _ in MACHINES]:
            raise Refused("machine type is not in the table")
        if gpu and which == "helper" and gpu != state["gpu"]:
            state["gpu"] = gpu                    # the fallback type the agent chose after a capacity failure
            cloud.put(f"deploy/{version}/state.json", json.dumps(state).encode())
        cloud.machine_up(which, state["gpu"])
    return {"machine": which, "state": cloud.machine_state(which)}


def start_step(cloud: Cloud, version: str, step: str) -> Dict[str, Any]:
    state = _state(cloud, version)
    if step not in STEPS or step == "pin":
        raise Refused("step must be one of: lock, build, register, check, package")
    return _start(cloud, state, version, step)


def _start(cloud: Cloud, state: Dict[str, Any], version: str, step: str) -> Dict[str, Any]:
    where, needs = STEPS[step]
    for earlier in needs:
        if _step_state(cloud, state, version, earlier)["state"] != "done":
            raise Refused(f"step {earlier} has not finished well yet")
    if _step_state(cloud, state, version, step)["state"] == "running":
        raise Refused("this step is already running")
    if cloud.machine_state(where) != "online":
        raise Refused(f"the {where} is not online; bring it up first")
    cloud.put(f"deploy/{version}/{step}.json", json.dumps({"ok": False, "exit": None}).encode())
    command = cloud.send_step(where, step, version, state["prefix"], state["model"], state["gpu"])
    state.setdefault("commands", {})[step] = {"id": command, "machine": where}
    cloud.put(f"deploy/{version}/state.json", json.dumps(state).encode())
    return {"step": step, "state": "running"}


def environment(cloud: Cloud) -> Dict[str, Any]:
    """What exists of the builder's side, and whether the build machine is prepared."""
    state = _json(cloud, f"deploy/{SETUP}/state.json") or {"commands": {}}
    return {"pieces": {name: cloud.stack_state(name) for name in PIECES},
            "build_machine": cloud.machine_state("builder") if cloud.stack_state("smh-poc-m1-builder") == "ready" else "absent",
            "preparation": _step_state(cloud, state, SETUP, "setup"),
            "missing_pieces_are_created_by": "the person's own command: bash smh.sh setup"}


def prepare_builder(cloud: Cloud) -> Dict[str, Any]:
    """Install the build tools, keys and driver on the build machine. One fixed script; safe to run again."""
    state = _json(cloud, f"deploy/{SETUP}/state.json") or {"version": SETUP, "model": "none/none", "prefix": "m0", "gpu": MACHINES[0][0], "commands": {}}
    if _step_state(cloud, state, SETUP, "setup")["state"] == "running":
        raise Refused("the preparation is already running")
    if cloud.machine_state("builder") != "online":
        raise Refused("the builder is not online; bring it up first with builder_power")
    cloud.put(f"deploy/{SETUP}/setup.json", json.dumps({"ok": False, "exit": None}).encode())
    command = cloud.send_step("builder", "setup", SETUP, state["prefix"], state["model"], state["gpu"])
    state["commands"]["setup"] = {"id": command, "machine": "builder"}
    cloud.put(f"deploy/{SETUP}/state.json", json.dumps(state).encode())
    return {"step": "setup", "state": "running", "takes": "about 15 minutes on a fresh machine, 1 minute when everything is already there"}


def builder_power(cloud: Cloud, action: str) -> Dict[str, Any]:
    """The build machine on or off, outside a release (first-time setup)."""
    if action not in ("up", "down"):
        raise Refused("action must be up or down")
    if cloud.stack_state("smh-poc-m1-builder") != "ready":
        raise Refused("there is no build machine yet; the person runs: bash smh.sh setup")
    if action == "down":
        state = _json(cloud, f"deploy/{SETUP}/state.json") or {"commands": {}}
        if _step_state(cloud, state, SETUP, "setup")["state"] == "running":
            raise Refused("the preparation is still running on the builder")
        cloud.machine_down("builder")
    else:
        cloud.machine_up("builder", MACHINES[0][0])
    return {"machine": "builder", "state": cloud.machine_state("builder")}


def get_status(cloud: Cloud, version: str) -> Dict[str, Any]:
    state = _state(cloud, version)
    return {"version": version, "model": state["model"], "folder": state["prefix"], "gpu": state["gpu"],
            "machines": {m: cloud.machine_state(m) for m in ("builder", "helper")},
            "steps": [_step_state(cloud, state, version, s) for s in STEPS],
            "approved": _approval_matches(cloud, version) is True}


def _summary_sha(cloud: Cloud, version: str) -> Optional[str]:
    raw = cloud.get(f"build/out-{version}/summary.json")
    return hashlib.sha256(raw).hexdigest() if raw else None


def _approval_matches(cloud: Cloud, version: str) -> Optional[bool]:
    """None: no approval record. False: a record for something else. True: this exact build."""
    approval = _json(cloud, f"approvals/{version}.json")
    sha = _summary_sha(cloud, version)
    if approval is None:
        return None
    return bool(sha) and approval.get("version") == version and approval.get("summary_sha256") == sha


def request_approval(cloud: Cloud, version: str) -> Dict[str, Any]:
    state = _state(cloud, version)
    steps = {s: _step_state(cloud, state, version, s) for s in ("build", "register", "check")}
    for name, st in steps.items():
        if st["state"] != "done":
            raise Refused(f"step {name} has not finished well yet")
    checks = steps["check"].get("checks") or {}
    not_passed = [c for c in MUST_PASS_BEFORE_APPROVAL if checks.get(c) != "PASS"]
    if not_passed:
        raise Refused("these checks have not passed: " + ", ".join(not_passed))
    sha = _summary_sha(cloud, version)
    if not sha or sha != steps["build"].get("summary_sha256"):
        raise Refused("the build's output file is missing or has changed since the build")
    request = {"version": version, "model": state["model"], "folder": state["prefix"], "gpu": state["gpu"],
               "ami": steps["register"].get("ami"), "summary_sha256": sha,
               **{k: steps["build"].get(k) for k in ("pcr4", "pcr7", "pcr12", "pcr15_open", "pcr15_locked", "packages", "image_check")},
               "checks": {c: checks.get(c) for c in MUST_PASS_BEFORE_APPROVAL},
               "effect": "after approval both key policies release only to this image; the image pinned now can no longer get the key"}
    cloud.put(f"deploy/{version}/approval-request.json", json.dumps(request, indent=1).encode())
    return {**request, "next": f"a human must review and run: bash deployer/approve.sh {version}"}


def pin_keys(cloud: Cloud, version: str) -> Dict[str, Any]:
    state = _state(cloud, version)
    matches = _approval_matches(cloud, version)
    if matches is None:
        raise Refused(f"not approved. A human must run: bash deployer/approve.sh {version}")
    if not matches:
        raise Refused("the approval on file is for a different build; it cannot be used for this one")
    return _start(cloud, state, version, "pin")


TOOLS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "profile_model": profile_model, "choose_machine": choose_machine, "start_deployment": start_deployment,
    "machine": machine, "start_step": start_step, "get_status": get_status,
    "request_approval": request_approval, "pin_keys": pin_keys,
    "environment": environment, "builder_power": builder_power, "prepare_builder": prepare_builder,
}

_V = {"type": "string", "description": "Version like 0.7.0"}
SCHEMAS: List[Dict[str, Any]] = [
    {"name": "profile_model", "description": "Size and parameter count of an open model on Hugging Face. Numbers only.",
     "inputSchema": {"type": "object", "properties": {"model": {"type": "string", "description": "owner/name"}}, "required": ["model"]}},
    {"name": "choose_machine", "description": "GPU machine types that can load a model of this size, in order of preference.",
     "inputSchema": {"type": "object", "properties": {"weights_bytes": {"type": "integer"}}, "required": ["weights_bytes"]}},
    {"name": "start_deployment", "description": "Open a deployment: version, model, a clean bucket folder (like m7) and the GPU type. Safe to call again to resume.",
     "inputSchema": {"type": "object", "properties": {"version": _V, "model": {"type": "string"}, "prefix": {"type": "string", "description": "clean folder, like m7"},
                                                     "gpu": {"type": "string"}}, "required": ["version", "model", "prefix", "gpu"]}},
    {"name": "machine", "description": "Bring the builder or the GPU helper up or down. For the helper, 'gpu' may name the next type from choose_machine after a capacity failure.",
     "inputSchema": {"type": "object", "properties": {"version": _V, "which": {"type": "string", "enum": ["builder", "helper"]},
                                                     "action": {"type": "string", "enum": ["up", "down"]}, "gpu": {"type": "string"}},
                     "required": ["version", "which", "action"]}},
    {"name": "start_step", "description": "Start one fixed step. lock (on the helper): encrypt weights and containers. build, register, check, package (on the builder). Each needs the earlier ones done.",
     "inputSchema": {"type": "object", "properties": {"version": _V, "step": {"type": "string", "enum": ["lock", "build", "register", "check", "package"]}},
                     "required": ["version", "step"]}},
    {"name": "get_status", "description": "Machines, every step's state and result fields, and whether a human has approved this build.",
     "inputSchema": {"type": "object", "properties": {"version": _V}, "required": ["version"]}},
    {"name": "request_approval", "description": "Prepare the summary a human must approve before the keys are pinned. Needs build, register and check done.",
     "inputSchema": {"type": "object", "properties": {"version": _V}, "required": ["version"]}},
    {"name": "pin_keys", "description": "Write both key policies for this build from the pipeline's own output. Refuses unless a human has approved exactly this build.",
     "inputSchema": {"type": "object", "properties": {"version": _V}, "required": ["version"]}},
    {"name": "environment", "description": "First-time setup: which fixed pieces of the builder's side exist, the build machine's state, and the result of its preparation (ready, keys, kernel, driver, lease_secret).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "builder_power", "description": "First-time setup only: bring the build machine up or down outside a release.",
     "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["up", "down"]}}, "required": ["action"]}},
    {"name": "prepare_builder", "description": "First-time setup: run the ONE fixed preparation script on the build machine (build tools, Secure Boot keys, NVIDIA driver for the image's kernel, lease secret). It never replaces a key. Safe to run again. The builder must be online.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def call(cloud: Cloud, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """One tool call. Never raises: the agent gets a fixed-form refusal instead."""
    fn = TOOLS.get(tool)
    if fn is None:
        return {"refused": "there is no such tool"}
    try:
        return fn(cloud, **arguments)
    except Refused as error:
        return {"refused": str(error)}
    except TypeError:
        return {"refused": "wrong or missing inputs for this tool"}
    except Exception as error:  # never leak details of a fault to the agent
        return {"refused": f"the tool failed ({type(error).__name__}); a human should look at the function's log"}

"""The watcher: when a step of smh.sh fails, find out why, fix what is safe to fix, and tell the human the rest.

    python3 agent/fixer.py --role builder|customer --step <name> --log <file> --name <stack prefix> --version <release>

It works for both sides. The script (smh.sh) runs each step; on a failure it
calls this program with the step's output. An AI model (Claude, on Amazon
Bedrock) then investigates with a few READ-ONLY tools and ends with one
decision: an action from a short fixed list, or exact instructions for a
person. smh.sh carries out the action and runs the step again.

What keeps this safe:
- The model can only look. Every tool below reads; none changes anything.
- It cannot run commands. It picks ONE action from ACTIONS; smh.sh owns the
  code of each action. An action that is not allowed for the role becomes
  "ask_human".
- If the model cannot be reached, plain rules (`rules`) give the answer, so a
  failure is never left unexplained.

Prints a short report, then a last line `ACTION <name> [value]` for smh.sh.
Every case is saved under evidence/watcher/. Tests: agent/test_fixer.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List

ACCOUNT, REGION, PROFILE = "111122223333", "us-east-1", "poc-ino"
# Which Claude model on Amazon Bedrock the watcher thinks with: an inference profile ID or ARN that YOUR account
# may use. It is a setting, not part of the code: SMH_AGENT_MODEL in smh.env. Nothing is created for it.
PLACEHOLDER = "REPLACE_WITH_YOUR_BEDROCK_INFERENCE_PROFILE"
_PROFILE_ARN = re.compile(r"arn:aws:bedrock:[a-z0-9-]+:[0-9]{12}:(?:application-)?inference-profile/[A-Za-z0-9._:-]+")


def model_setting(env: Dict[str, str], dotenv_text: str = "") -> str:
    """The model to use: SMH_AGENT_MODEL, or an inference profile found in the local .env file. Never hard-coded."""
    value = (env.get("SMH_AGENT_MODEL") or "").strip()
    if value and value != PLACEHOLDER:
        return value
    found = _PROFILE_ARN.search(dotenv_text)
    if found:
        return found.group(0)
    for line in dotenv_text.splitlines():           # or a plain  name=value  line
        name, _, val = line.partition("=")
        if name.strip().lower() in ("smh_agent_model", "inference_profile", "infrence_profile") and val.strip():
            return val.strip().strip("\"'")
    return ""


def _dotenv() -> str:
    try:
        with open(".env", errors="replace") as f:
            return f.read(4000)
    except OSError:
        return ""


MODEL = model_setting(dict(os.environ), _dotenv())
MACHINE_TYPES = ["g5.xlarge", "g6.xlarge", "g5.2xlarge", "g6.2xlarge", "g5.4xlarge"]

# action -> (who may use it, what smh.sh does)
ACTIONS = {
    "retry": ("both", "run the same step again now"),
    "wait_retry": ("both", "wait `value` minutes (1 to 15), then run the step again; for things that pass by themselves, like no free GPU machine"),
    "other_machine_type": ("customer", "run the step again with the GPU machine type in `value`"),
    "clean_failed_stack": ("customer", "delete the customer's own half-made stack (only if it is in a failed state), then run the step again"),
    "restart_machine": ("customer", "stop and start the sealed machine, wait for the model, then run the step again"),
    "new_api_key": ("customer", "make a new API key in the customer's bucket, then run the step again"),
    "onboard": ("builder", "give the customer's machine role access on both keys (the measurements are still required), then run the step again"),
    "ask_human": ("both", "stop and show `human_steps`; the step runs again when the person says it is done"),
}
CATEGORIES = ["capacity", "permission", "model", "configuration", "key_access", "network", "limit", "bug", "unknown"]

PLAYBOOK = """
Known causes in this system (use them, but check with the tools before you decide):
- No free GPU machine: text like "capacity", "InsufficientInstanceCapacity", "failed:capacity". Passes by itself. Customer: other_machine_type, then wait_retry. Builder: wait_retry.
- Sealed machine boots but "key release failed" / "AccessDenied" on the first key request: the customer's machine role is not onboarded on the builder's keys, or the keys are tied to another release. Builder side can fix with onboard. Customer side cannot: ask_human, and say exactly what to ask the builder for.
- "LEASE refused": the builder took access away, or disabled the keys. Customer: ask_human (contact the builder). Builder: check key_access.
- The machine powered off about 90 seconds after boot: no model came up; the machine log says why. After the cause is fixed the customer needs restart_machine.
- "The maximum number of VPCs has been reached" or another "LimitExceeded": an account limit. Nobody can fix it from here: ask_human with the limit's name and where to raise it or what to free.
- "is not authorized to perform", "AccessDenied", "UnauthorizedOperation" when the PERSON's command runs: the person's AWS user lacks a permission. Use my_permissions to name the exact missing actions. ask_human with that list for their AWS administrator.
- A stack in ROLLBACK_COMPLETE / CREATE_FAILED / DELETE_FAILED blocks the next try: clean_failed_stack (customer's own stack only), after you have read WHY it failed with stack_failures.
- Endpoint answers 401 to everything: no API key, or a key of the wrong shape (24 to 128 letters, digits, - or _). new_api_key.
- Model-specific problems (builder, during a release): the model is gated or private on Hugging Face, has no .safetensors weights, is not a text-generation model, or is too big (loading needs about twice the weights' size in GPU memory plus 2 GiB; the largest allowed machine has 48 GiB). Use model_info. These need a human decision: ask_human, and say which model property is the problem and what would work. The build downloads with NO Hugging Face token and has no place to put one today, so for a gated or private model say that plainly. The ways forward are: pick a public model; or, if the builder has the weights themselves (their own trained model, or weights they are licensed to hold), ship them from a folder with  bash smh.sh ship <folder>  (a Hugging Face style folder with config.json, tokenizer files and .safetensors weights; it may be on a disk, a network share, or an s3:// location). A lock step that prints a line starting with MODEL PROBLEM names the cause itself.
- "the folder is not clean" / "this version already exists with another model": a release folder or version was reused. ask_human: pick a new version (the next patch number) or a new folder.
- The release agent says STUCK during the "lock" step before 45 minutes: it gave up early. retry.
- A fine-tune job comes back "refused" with a list of problems: the form broke a builder limit (rank up to 16, steps 1 to 2000, data file up to 50 MB, 5 jobs a day, known fields only). ask_human: name the limit and give the corrected command, which has the shape  bash smh.sh finetune <file.jsonl> <steps>  (steps is optional; 150 is used when left out).
- The release agent reports the GPU helper as "failed:other" straight after another failure: the previous helper machine is usually still being removed (that takes a few minutes). wait_retry 5.
- A step log can be from an EARLIER try of the same release. If the release notes say the step was not run again this time, say so; do not present the old error as new.
- Required tags missing ("explicit deny", "not authorized ... with an explicit deny in a service control policy"): the organization refuses untagged resources. ask_human.
Rules:
- Prefer an action that fixes the cause. Use retry only when you have a reason the same step would now pass.
- Never suggest turning a protection off (login, firewall, measurements, key rules), sharing a key, or widening a role.
- If you are not sure, ask_human and say what you saw and what you could not find out.
- The AWS account may be shared with other teams. Never tell anyone to delete, stop or change something this project did not create, and never suggest using another person's AWS user. For a network (VPC) limit, the options are: ask the administrator to raise the limit, or build inside an existing network of this project with the settings described in smh.env.example.
- human_steps are for a person who may not be technical: numbered, exact, one thing each, with the exact command or the exact words to send to someone.
"""


def clip(text: Any, n: int = 3500) -> str:
    text = str(text)
    return text if len(text) <= n else text[: n // 2] + "\n...\n" + text[-n // 2:]


# ---------------------------------------------------------------------------
# Read-only tools. Each takes the model's input and returns text or JSON.
# ---------------------------------------------------------------------------

def make_tools(session: Any, args: argparse.Namespace) -> Dict[str, Callable[[Dict[str, Any]], Any]]:
    cf, ec2, iam, sts = (session.client(n) for n in ("cloudformation", "ec2", "iam", "sts"))
    releasing = os.environ.get("SMH_RELEASING") or args.version      # a release being made, else the current one

    def own_stack(name: str) -> str:
        name = str(name or args.name)
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}", name) or not (name.startswith(args.name) or name.startswith("smh-poc-")):
            raise ValueError("only this project's stacks can be read")
        return name

    def stack_failures(i: Dict[str, Any]) -> Any:
        name = own_stack(i.get("stack", ""))
        try:
            status = cf.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except Exception as error:
            return {"stack": name, "status": "does not exist", "note": str(error)[:120]}
        events = cf.describe_stack_events(StackName=name)["StackEvents"][:60]
        failed = [{"resource": e["LogicalResourceId"], "status": e["ResourceStatus"], "reason": e.get("ResourceStatusReason", "")[:300]}
                  for e in events if "FAILED" in e["ResourceStatus"]][:8]
        return {"stack": name, "status": status, "failures_newest_first": failed}

    def machine_log(_: Dict[str, Any]) -> Any:
        outs = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=own_stack(args.name))["Stacks"][0].get("Outputs", [])}
        iid = outs.get("InstanceId")
        if not iid:
            return "the customer's stack has no machine"
        inst = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
        text = ec2.get_console_output(InstanceId=iid, Latest=True).get("Output", "") or ""
        lines = [re.sub(r"^\[\s*[\d.]+\]\s*", "", ln)[:220] for ln in text.splitlines() if "SMH" in ln]
        return {"machine": iid, "type": inst["InstanceType"], "state": inst["State"]["Name"], "log_tail": lines[-45:] or ["(no lines from our software yet)"]}

    def release_notes(i: Dict[str, Any]) -> Any:
        version = str(i.get("version") or releasing)
        if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version):
            raise ValueError("bad version")
        path = f"evidence/releases/agent-{version}.txt"
        if not os.path.exists(path):
            return f"no notes for release {version}"
        with open(path, errors="replace") as f:
            return clip("".join(f.readlines()[-70:]), 5000)

    def step_log(i: Dict[str, Any]) -> Any:
        version, step = str(i.get("version") or releasing), str(i.get("step", ""))
        if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", version) or step not in ("lock", "build", "register", "check", "package", "pin"):
            raise ValueError("version like 0.10.1 and step one of lock, build, register, check, package, pin")
        body = session.client("s3").get_object(Bucket=f"smh-poc-artifacts-{ACCOUNT}", Key=f"deploy/{version}/logs/{step}.log")["Body"].read()[-9000:]
        return clip("\n".join(body.decode("utf-8", "replace").splitlines()[-60:]), 6000)

    def key_access(_: Dict[str, Any]) -> Any:
        out: Dict[str, Any] = {}
        for stack, alias in (("smh-poc-m1-key", "alias/smh-poc-model-key"), ("smh-poc-m1-lease", "alias/smh-poc-lease-key")):
            p = {x["ParameterKey"]: x.get("ParameterValue", "") for x in cf.describe_stacks(StackName=stack)["Stacks"][0]["Parameters"]}
            state = session.client("kms").describe_key(KeyId=alias)["KeyMetadata"]["KeyState"]
            out[alias] = {"state": state, "tied_to_measurement_PCR4": p.get("Pcr4", "")[:16], "customer_role_with_access": p.get("CustomerRoleArn") or "nobody"}
        try:
            with open(f"evidence/releases/build-{args.version}/summary.json") as f:
                out["current_release"] = {"version": args.version, "PCR4": json.load(f)["PCR4"][:16]}
        except OSError:
            out["current_release"] = {"version": args.version, "PCR4": "unknown on this laptop"}
        out["expected_customer_role"] = f"arn:aws:iam::{ACCOUNT}:role/zl/{args.name}-sealed-role"
        return out

    def my_permissions(i: Dict[str, Any]) -> Any:
        actions = [a for a in i.get("actions", []) if re.fullmatch(r"[a-z0-9-]+:[A-Za-z*]+", str(a))][:20]
        arn = sts.get_caller_identity()["Arn"]
        m = re.fullmatch(r"arn:aws:sts::(\d{12}):assumed-role/([^/]+)/.+", arn)
        if m:       # signed in through a role (SSO, a federated login): the check needs the role itself, with its path
            arn = iam.get_role(RoleName=m.group(2))["Role"]["Arn"]
        res = iam.simulate_principal_policy(PolicySourceArn=arn, ActionNames=actions)["EvaluationResults"]
        return {"who": arn, "results": {r["EvalActionName"]: r["EvalDecision"] for r in res},
                "note": "this checks the user's own policy only; an organization rule (SCP) can still refuse, for example when the six tags are missing"}

    def account_limits(_: Dict[str, Any]) -> Any:
        vpcs = ec2.describe_vpcs()["Vpcs"]
        try:
            limit = session.client("service-quotas").get_service_quota(ServiceCode="vpc", QuotaCode="L-F678F1CE")["Quota"]["Value"]
        except Exception:
            limit = "unknown"
        return {"networks_in_use": len(vpcs), "network_limit": limit, "ours": sum(1 for v in vpcs if any(t["Key"] == "app" and t["Value"] == "Secure-model-hosting" for t in v.get("Tags", [])))}

    def model_info(i: Dict[str, Any]) -> Any:
        import urllib.error
        import urllib.request

        model = str(i.get("model", ""))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,90}", model):
            raise ValueError("model must look like owner/name")
        try:
            if model.startswith("local/"):      # the builder's own weights: read what was staged, not Hugging Face
                try:
                    d = json.loads(session.client("s3").get_object(Bucket=f"smh-poc-artifacts-{ACCOUNT}", Key=f"deploy/models-in/{model[6:]}/model-info.json")["Body"].read())
                except Exception:
                    return {"model": model, "source": "the builder's own weights", "note": "nothing is staged under that name now (the staged copy is deleted after a successful encryption step)"}
            else:
              with urllib.request.urlopen(f"https://huggingface.co/api/models/{model}?blobs=true", timeout=20) as r:
                d = json.load(r)
        except urllib.error.HTTPError as error:
            return {"model": model, "error": f"Hugging Face answered {error.code}", "meaning": "401 or 403: private or gated; 404: no such model"}
        weights = sum(int(s.get("size") or 0) for s in d.get("siblings", []) if str(s.get("rfilename", "")).endswith(".safetensors"))
        gib = weights / (1 << 30)
        return {"model": model, "gated": d.get("gated"), "private": d.get("private"), "task": d.get("pipeline_tag"), "library": d.get("library_name"),
                "architectures": (d.get("config") or {}).get("architectures"), "safetensors_weights_gib": round(gib, 2),
                "gpu_memory_needed_gib": round(gib * 2 + 2, 1), "largest_allowed_gpu_gib": 48,
                "license": (d.get("cardData") or {}).get("license")}

    return {"stack_failures": stack_failures, "machine_log": machine_log, "release_notes": release_notes, "step_log": step_log, "key_access": key_access,
            "my_permissions": my_permissions, "account_limits": account_limits, "model_info": model_info}


TOOL_SPECS = [
    ("stack_failures", "Why a CloudFormation stack of this project failed: its status and the newest failed events with AWS's own reason.",
     {"stack": {"type": "string", "description": "stack name; empty = the customer's stack"}}),
    ("machine_log", "The sealed machine's state and the last lines its own software wrote while booting (key request, lease, model, API keys).", {}),
    ("release_notes", "The end of the release agent's working notes for a release (builder side).", {"version": {"type": "string"}}),
    ("step_log", "The end of the real log of one release step as it ran on the build machines (builder side). This is where the actual error of a failed step is: use it whenever the agent's notes say a step failed.",
     {"version": {"type": "string"}, "step": {"type": "string", "enum": ["lock", "build", "register", "check", "package", "pin"]}}),
    ("key_access", "Both keys: enabled or not, which release they are tied to, which customer role has access, and what the current release and the expected customer role are.", {}),
    ("my_permissions", "Whether the person's own AWS user is allowed a list of actions, for example ['ec2:RunInstances','cloudformation:CreateStack'].",
     {"actions": {"type": "array", "items": {"type": "string"}}}),
    ("account_limits", "Account limits that have stopped this before: networks (VPCs) in use against the limit.", {}),
    ("model_info", "Facts about a Hugging Face model that decide if it can be released: gated or private, task, architecture, size of its safetensors weights, GPU memory needed.",
     {"model": {"type": "string", "description": "owner/name"}}),
]
DECIDE = {"category": {"type": "string", "enum": CATEGORIES},
          "what_happened": {"type": "string", "description": "one or two plain sentences: the cause, and how you know"},
          "action": {"type": "string", "enum": list(ACTIONS)},
          "value": {"type": "string", "description": "minutes for wait_retry, machine type for other_machine_type, else empty"},
          "human_steps": {"type": "array", "items": {"type": "string"}, "description": "only for ask_human: exact numbered steps for a person"},
          "sure": {"type": "string", "enum": ["yes", "mostly", "no"]}}


def checked(verdict: Dict[str, Any], role: str) -> Dict[str, Any]:
    """The model's decision, made safe: an action it may not take becomes a request to a person."""
    v = {"category": verdict.get("category") if verdict.get("category") in CATEGORIES else "unknown",
         "what_happened": clip(verdict.get("what_happened") or "The cause could not be found.", 600),
         "action": str(verdict.get("action") or "ask_human"), "value": str(verdict.get("value") or "").strip(),
         "human_steps": [clip(s, 400) for s in (verdict.get("human_steps") or []) if str(s).strip()][:8],
         "sure": verdict.get("sure") if verdict.get("sure") in ("yes", "mostly", "no") else "no"}
    who = ACTIONS.get(v["action"], ("nobody", ""))[0]
    if who not in ("both", role):
        v["human_steps"] = v["human_steps"] or [f"The fix \"{v['action']}\" is not something the {role} side may do. Ask the other side to do it."]
        v["action"] = "ask_human"
    if v["action"] == "wait_retry":
        v["value"] = str(min(max(int(v["value"]) if v["value"].isdigit() else 10, 1), 15))
    elif v["action"] == "other_machine_type":
        if v["value"] not in MACHINE_TYPES:
            v["value"] = ""
    else:
        v["value"] = ""
    if v["action"] == "ask_human" and not v["human_steps"]:
        v["human_steps"] = ["Read the lines above this report, fix what they name, then continue."]
    return v


def rules(log: str, role: str) -> Dict[str, Any]:
    """Plain rules, used when the model cannot be reached. Coarser, but never silent."""
    t = log.lower()
    if "capacity" in t:
        return {"category": "capacity", "what_happened": "AWS had no free GPU machine of the type asked for.", "action": "wait_retry", "value": "10", "sure": "mostly"}
    if "maximum number of vpcs" in t or "limitexceeded" in t:
        return {"category": "limit", "what_happened": "An AWS account limit was reached.", "action": "ask_human", "sure": "mostly",
                "human_steps": ["Ask your AWS administrator to raise the limit named in the lines above, or free one of those resources.", "Then continue."]}
    if "key release failed" in t or "refused the key" in t:
        if role == "builder":
            return {"category": "key_access", "what_happened": "The sealed machine was refused the key: the customer's role has no access on the keys.", "action": "onboard", "sure": "mostly"}
        return {"category": "key_access", "what_happened": "The sealed machine was refused the key by the builder's key service.", "action": "ask_human", "sure": "mostly",
                "human_steps": ["Send the builder this: \"Please give my machine role access: bash smh.sh onboard\".", "When they confirm, continue: the machine is restarted for you."]}
    if "not authorized" in t or "accessdenied" in t or "unauthorizedoperation" in t:
        return {"category": "permission", "what_happened": "AWS refused an action for lack of permission.", "action": "ask_human", "sure": "mostly",
                "human_steps": ["Send your AWS administrator the line above that says \"not authorized\" or \"AccessDenied\"; it names the action that is missing.", "When it is granted, continue."]}
    if "rollback_complete" in t or "delete_failed" in t:
        return {"category": "configuration", "what_happened": "An earlier try left a half-made stack.", "action": "clean_failed_stack" if role == "customer" else "ask_human", "sure": "mostly"}
    if "-> 401" in t:
        return {"category": "configuration", "what_happened": "The endpoint refused the API key.", "action": "new_api_key" if role == "customer" else "ask_human", "sure": "mostly"}
    return {"category": "unknown", "what_happened": "The step failed and the cause is not one of the known ones.", "action": "ask_human", "sure": "no",
            "human_steps": ["Read the last lines above this report.", "If they name something you can fix, fix it and continue. Otherwise send those lines to the project owner."]}


def investigate(session: Any, args: argparse.Namespace, log: str, say: Callable[[str], None]) -> Dict[str, Any]:
    """Let the model look around with the read-only tools until it calls `decide`."""
    tools = make_tools(session, args)
    allowed = {k: v[1] for k, v in ACTIONS.items() if v[0] in ("both", args.role)}
    system = (f"You watch an automated setup of 'sealed model hosting' and are called when a step fails. You help the {args.role.upper()} side "
              f"({'the party that owns the model and releases it' if args.role == 'builder' else 'the party that runs the model in its own AWS account'}). "
              "Find the real cause using the tools (they only read), then call `decide` exactly once. Be brief and concrete.\n"
              f"Actions you may choose (the script carries them out, you cannot run anything yourself):\n{json.dumps(allowed, indent=1)}\n{PLAYBOOK}")
    specs = [{"toolSpec": {"name": n, "description": d, "inputSchema": {"json": {"type": "object", "properties": p}}}} for n, d, p in TOOL_SPECS]
    specs.append({"toolSpec": {"name": "decide", "description": "Give the final decision. Call this once, last.",
                               "inputSchema": {"json": {"type": "object", "properties": DECIDE, "required": ["category", "what_happened", "action", "sure"]}}}})
    first = (f"Step that failed: {args.step}\nSide: {args.role}\nRelease: {args.version}\nCustomer stack name: {args.name}\n"
             f"Model being released (builder steps): {args.model or 'not given'}\nRelease being made (builder steps): {os.environ.get('SMH_RELEASING') or 'none'}\n"
             f"Try number: {args.attempt}\n\nThe step's output (end):\n{clip(log, 6000)}")
    messages: List[Dict[str, Any]] = [{"role": "user", "content": [{"text": first}]}]
    brt = session.client("bedrock-runtime")
    looked: List[str] = []
    for _ in range(8):
        reply = brt.converse(modelId=MODEL, system=[{"text": system}], messages=messages, toolConfig={"tools": specs},
                             inferenceConfig={"maxTokens": 1500})
        message = reply["output"]["message"]
        messages.append(message)
        uses = [c["toolUse"] for c in message["content"] if "toolUse" in c]
        if not uses:
            messages.append({"role": "user", "content": [{"text": "Call the decide tool now with your decision."}]})
            continue
        results = []
        for use in uses:
            if use["name"] == "decide":
                return dict(use["input"], looked_at=looked)
            looked.append(use["name"])
            say(f"  looking at: {use['name'].replace('_', ' ')}")
            try:
                out = tools[use["name"]](use.get("input") or {})
            except Exception as error:      # a tool that fails is information for the model, not a crash
                out = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
            results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"text": clip(json.dumps(out, default=str), 6000)}]}})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("no decision after 8 rounds")


def report(v: Dict[str, Any], role: str) -> List[str]:
    lines = ["", "  ---------------- WATCHER ----------------",
             f"  What went wrong ({v['category']}): {v['what_happened']}"]
    if v["action"] == "ask_human":
        lines.append("  This needs a person. Do this:")
        lines += [f"    {n}. {re.sub(r'^[0-9]+[.)] *', '', s)}" for n, s in enumerate(v["human_steps"], 1)]
    else:
        lines.append(f"  Fixing it myself: {ACTIONS[v['action']][1].replace('`value`', v['value'] or 'value')}.")
    lines.append("  ------------------------------------------")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    for name in ("role", "step", "log", "name", "version"):
        ap.add_argument("--" + name, required=True)
    ap.add_argument("--model", default="")
    ap.add_argument("--attempt", default="1")
    args = ap.parse_args()
    if args.role not in ("builder", "customer"):
        return 2
    with open(args.log, errors="replace") as f:
        log = re.sub(r"\x1b\[[0-9;]*m", "", f.read())[-12000:]

    def say(text: str) -> None:
        print(text, flush=True)

    source = "model"
    try:
        import boto3

        if not MODEL or MODEL == PLACEHOLDER:
            raise RuntimeError("SMH_AGENT_MODEL is not set")
        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
            raise RuntimeError("wrong account")
        verdict = investigate(session, args, log, say)
    except Exception as error:
        why = "set SMH_AGENT_MODEL in smh.env to a Bedrock inference profile your account may use" if "SMH_AGENT_MODEL" in str(error) else type(error).__name__
        say(f"  (the watcher's AI model is not available: {why}; using plain rules instead)")
        verdict, source = rules(log, args.role), "rules"
    v = checked(verdict, args.role)
    for line in report(v, args.role):
        say(line)
    os.makedirs("evidence/watcher", exist_ok=True)
    with open(f"evidence/watcher/{time.strftime('%Y%m%d-%H%M%S')}-{args.role}-{re.sub(r'[^a-z0-9]+', '-', args.step.lower())}.json", "w") as f:
        json.dump({"role": args.role, "step": args.step, "attempt": args.attempt, "decided_by": source, "decision": v,
                   "looked_at": verdict.get("looked_at"), "log_tail": log[-3000:]}, f, indent=1)
    say(f"ACTION {v['action']} {v['value']}".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

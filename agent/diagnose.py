"""The Lambda function behind the watcher's AgentCore Gateway: fixer.py's read-only tools, plus `decide`.

Its role can only read (stack events, the machine's console, step logs, key state, permissions, limits) and write
ONE kind of object: the record of a decision, under deploy/watch/ in the builder's bucket. Every call is also
written to this function's log group, so what the agent looked at can be read back.
"""
import argparse
import json
import os
import re
import time

import boto3

import fixer

BUCKET = os.environ["BUCKET"]
session = boto3.Session()


def run(name, event):
    if name == "decide":
        side = event.get("side")
        if side not in ("builder", "customer"):
            raise ValueError("side must be builder or customer")
        verdict = fixer.checked(event, side)
        step = re.sub(r"[^a-z0-9]+", "-", str(event.get("step", "")).lower())[:60]
        key = f"deploy/watch/{time.strftime('%Y%m%d-%H%M%S')}-{side}-{step}.json"
        session.client("s3").put_object(Bucket=BUCKET, Key=key, Body=json.dumps({"side": side, "step": event.get("step"), "decision": verdict}, indent=1).encode())
        return dict(verdict, record=key)
    stack = str(event.get("stack") or "smh-poc-oc")
    if not stack.startswith("smh-poc-"):        # the role says the same; this is the second lock
        raise ValueError("only this project's stacks can be read")
    args = argparse.Namespace(name=stack, version=str(event.get("version") or "0.0.0"), caller="")
    tools = fixer.make_tools(session, args)
    if name not in tools or name == "release_notes":
        raise ValueError("no such tool")
    if name == "my_permissions" and not event.get("who"):
        raise ValueError("pass `who`: the person's ARN from the first message")
    return tools[name](event)


def lambda_handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    event = event if isinstance(event, dict) else {}
    name = str(custom.get("bedrockAgentCoreToolName") or event.pop("__tool", "")).split("___")[-1]
    try:
        result = run(name, event)
    except Exception as error:      # a tool that fails is information for the model, not a crash
        result = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
    print(json.dumps({"tool": name, "inputs": event, "error": result.get("error") if isinstance(result, dict) else None}, default=str)[:3000])
    return result if isinstance(result, dict) else {"text": fixer.clip(result, 6000)}

"""Talk to the deployer agent and keep asking it to continue until it stops.

    python3 deployer/run.py "<message>" [--session <id>] [--rounds N] [--pause seconds] [--out evidence/m7/<name>.txt]

Each turn is saved in full (text, tool calls, tool results) to the --out file.
The agent ends every turn with a STATUS line; WORKING means ask again later.
"""
import argparse
import json
import os
import sys
import time
import uuid

import boto3

p = argparse.ArgumentParser()
p.add_argument("message"); p.add_argument("--session", default=str(uuid.uuid4()))
p.add_argument("--rounds", type=int, default=1); p.add_argument("--pause", type=int, default=120)
p.add_argument("--out", default=None)
p.add_argument("--harness", default="deployer/.harness")       # which agent: the file deployer/setup.sh wrote for it
p.add_argument("--again", default="Continue. Start with get_status.")
args = p.parse_args()
arn = open(args.harness).read().split()[-1]
client = boto3.Session(profile_name=os.environ.get("SMH_PROFILE", "poc-ino"), region_name=os.environ.get("SMH_REGION", "us-east-1")).client("bedrock-agentcore")
out = open(args.out, "a") if args.out else None


def log(line=""):
    print(line, flush=True)
    if out:
        out.write(line + "\n"); out.flush()


def turn(message):
    log(f"\n===== {time.strftime('%H:%M:%S', time.gmtime())} UTC  session {args.session}\nHUMAN: {message}\nAGENT:")
    text, tool, said = "", "", ""
    stream = client.invoke_harness(harnessArn=arn, runtimeSessionId=args.session,
                                   messages=[{"role": "user", "content": [{"text": message}]}])["stream"]
    for event in stream:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tool = f"  [tool call] {start['toolUse'].get('name')} "
            if "toolResult" in start:
                tool = "  [tool result] "
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text += delta["text"]; said += delta["text"]
            elif "toolUse" in delta:
                tool += str(delta["toolUse"].get("input", ""))
            elif "toolResult" in delta:
                tool += json.dumps(delta["toolResult"], default=str)[:1500]
        elif "contentBlockStop" in event:
            if tool:
                log(tool); tool = ""
            elif text.strip():
                log(text.strip()); text = ""
        elif "messageStop" in event:
            log(f"  [stop: {event['messageStop'].get('stopReason')}]")
        elif any(k.endswith("Exception") or k == "runtimeClientError" for k in event):
            log(f"  [error] {json.dumps(event, default=str)[:400]}")
    if text.strip():
        log(text.strip())
    last = [l for l in said.strip().splitlines() if l.startswith("STATUS:")]
    return last[-1].split(":", 1)[1].strip() if last else "UNKNOWN"


status = turn(args.message)
for _ in range(args.rounds - 1):
    if status != "WORKING":
        break
    time.sleep(args.pause)
    status = turn(args.again)
log(f"\nFINAL STATUS: {status}   (session {args.session})")
sys.exit(0 if status in ("DONE", "NEEDS_APPROVAL", "WORKING") else 1)

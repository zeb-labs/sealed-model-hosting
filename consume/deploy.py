"""Put the agent (consume/main.py) on AgentCore Runtime in the customer's network, talk to it, remove it.

    python consume/deploy.py up <customer stack>        about 5 minutes; safe to run again (it updates)
    python consume/deploy.py ask <customer stack> "<question>"
    python consume/deploy.py remove <customer stack>

`up` reads the customer's stack (endpoint, bucket, subnet), reads the sealed machine's certificate fingerprint
from the machine's own log, makes the agent's role and security group (stack <name>-agent), uploads the one-file
code package, and creates or updates the AgentCore Runtime in VPC mode. smh.sh calls this (agent-up, ask, agent-remove).
"""
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile

ACCOUNT, REGION, PROFILE = (os.environ.get("SMH_ACCOUNT", "111122223333"), os.environ.get("SMH_REGION", "us-east-1"),
                            os.environ.get("SMH_PROFILE", "poc-ino"))
OWNER = os.environ.get("SMH_OWNER", "owner@example.com")
CODE_BUCKET = f"smh-poc-artifacts-{ACCOUNT}"
TAGS = {"business-unit": "ai", "env": "innovation", "category": "rnd", "owner": OWNER, "app": "Secure-model-hosting", "expire-date": "recurring"}
NAME = re.compile(r"smh-poc-oc[a-z0-9-]{0,12}")
HERE = os.path.dirname(os.path.abspath(__file__))


def runtime_name(stack):
    return "smh_poc_agent_" + re.sub(r"[^a-z0-9]", "_", stack[len("smh-poc-"):])


def fingerprint(console):
    """The newest certificate fingerprint the sealed machine printed (it makes a new certificate at every boot)."""
    found = re.findall(r"SMH GATE https on \S+ cert_sha256=([0-9A-Fa-f]{64})", console or "")
    return found[-1].lower() if found else None


def package():
    """The deployment package: the agent (one file) plus the AWS library it needs to read the customer's key file.
    AgentCore Runtime's Python has no boto3 of its own (probe, 27 Nov). The library is pure Python, so the copy
    already installed here is used as it is; of its service descriptions only S3 and STS are taken."""
    import importlib
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        def add(path, name):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, "rb") as f:
                z.writestr(info, f.read())
        add(os.path.join(HERE, "main.py"), "main.py")
        for module in ("boto3", "botocore", "s3transfer", "jmespath", "dateutil", "urllib3", "six"):
            origin = importlib.import_module(module).__file__
            if os.path.basename(origin) != "__init__.py":           # a single-file module
                add(origin, os.path.basename(origin))
                continue
            root = os.path.dirname(origin)
            for folder, _, files in os.walk(root):
                rel = os.path.relpath(folder, os.path.dirname(root))
                parts = rel.split(os.sep)
                if "__pycache__" in parts or (parts[:2] == ["botocore", "data"] and len(parts) > 2 and parts[2] not in ("s3", "sts")):
                    continue
                for name in files:
                    if not name.endswith(".pyc"):
                        add(os.path.join(folder, name), f"{rel}/{name}")
    return buffer.getvalue()


def outputs(cf, stack):
    return {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])}


def find(control, name):
    token = None
    while True:
        page = control.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for r in page.get("agentRuntimes", []):
            if r["agentRuntimeName"] == name:
                return r
        token = page.get("nextToken")
        if not token:
            return None


def up(session, stack):
    cf, ec2, s3 = session.client("cloudformation"), session.client("ec2"), session.client("s3")
    control = session.client("bedrock-agentcore-control")
    o = outputs(cf, stack)
    pin = fingerprint(ec2.get_console_output(InstanceId=o["InstanceId"], Latest=True).get("Output", ""))
    if not pin:
        sys.exit("The sealed machine has not printed its certificate yet. Is it up?  bash smh.sh up")
    print(f"sealed endpoint {o['Endpoint']}, certificate {pin[:16]}...")
    with open(os.path.join(HERE, "..", "infra", "oneclick", "consume.yaml")) as f:
        template = f.read()
    params = [{"ParameterKey": k, "ParameterValue": v} for k, v in {"Name": stack, "VpcId": o["VpcId"], "CustomerBucket": o["Bucket"],
                                                                     "CustomerKeyArn": o["KeyArn"], "CodeBucket": CODE_BUCKET}.items()]
    kwargs = dict(StackName=stack + "-agent", TemplateBody=template, Parameters=params, Capabilities=["CAPABILITY_NAMED_IAM"],
                  Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])
    try:
        cf.create_stack(**kwargs)
        cf.get_waiter("stack_create_complete").wait(StackName=stack + "-agent")
        print("made the agent's role and security group")
    except cf.exceptions.AlreadyExistsException:
        try:
            cf.update_stack(**kwargs)
            cf.get_waiter("stack_update_complete").wait(StackName=stack + "-agent")
        except Exception as error:
            if "No updates" not in str(error):
                raise
    a = outputs(cf, stack + "-agent")
    key = f"consume/{stack}/package.zip"
    s3.put_object(Bucket=CODE_BUCKET, Key=key, Body=package())
    spec = dict(
        agentRuntimeArtifact={"codeConfiguration": {"code": {"s3": {"bucket": CODE_BUCKET, "prefix": key}}, "runtime": "PYTHON_3_13", "entryPoint": ["main.py"]}},
        networkConfiguration={"networkMode": "VPC", "networkModeConfig": {"subnets": [o["SubnetId"]], "securityGroups": [a["SecurityGroup"]]}},
        roleArn=a["RoleArn"],
        environmentVariables={"SMH_ENDPOINT": o["Endpoint"], "SMH_CERT_SHA256": pin, "SMH_MODEL": os.environ.get("SMH_MODEL", "demo"),
                              "SMH_KEY_BUCKET": o["Bucket"], "AWS_DEFAULT_REGION": REGION},
        description="An agent whose model is the sealed model (smh-poc)")
    name, existing = runtime_name(stack), None
    existing = find(control, name)
    if existing:
        result = control.update_agent_runtime(agentRuntimeId=existing["agentRuntimeId"], **spec)
    else:
        result = control.create_agent_runtime(agentRuntimeName=name, tags=TAGS, **spec)
    rid = result["agentRuntimeId"]
    for _ in range(90):
        status = control.get_agent_runtime(agentRuntimeId=rid)["status"]
        if status == "READY":
            break
        if status.endswith("FAILED"):
            sys.exit(f"AgentCore Runtime: {status}: {control.get_agent_runtime(agentRuntimeId=rid).get('failureReason', '')[:300]}")
        time.sleep(10)
    else:
        sys.exit("AgentCore Runtime did not become ready in 15 minutes")
    with open(os.path.join(HERE, f".runtime-{stack}"), "w") as f:
        f.write(result["agentRuntimeArn"])
    print(f"agent ready: {name} on AgentCore Runtime, inside the customer's network")


def ask(session, stack, prompt):
    with open(os.path.join(HERE, f".runtime-{stack}")) as f:
        arn = f.read().strip()
    reply = session.client("bedrock-agentcore").invoke_agent_runtime(
        agentRuntimeArn=arn, runtimeSessionId=str(uuid.uuid4()) + "-smh", contentType="application/json", accept="application/json",
        payload=json.dumps({"prompt": prompt}).encode())
    data = json.loads(reply["response"].read() or b"{}")
    if data.get("error"):
        sys.exit("The agent says: " + data["error"])
    for step in data.get("steps", []):
        print("  step:", step)
    print(data.get("result", ""))
    print(f"  ({data.get('served_by', '')}; model {data.get('model', '')})")


def remove(session, stack):
    cf, control = session.client("cloudformation"), session.client("bedrock-agentcore-control")
    existing = find(control, runtime_name(stack))
    if existing:
        control.delete_agent_runtime(agentRuntimeId=existing["agentRuntimeId"])
        for _ in range(60):
            if not find(control, runtime_name(stack)):
                break
            time.sleep(10)
        print("agent removed from AgentCore Runtime")
    session.client("s3").delete_object(Bucket=CODE_BUCKET, Key=f"consume/{stack}/package.zip")
    try:
        cf.delete_stack(StackName=stack + "-agent")
        cf.get_waiter("stack_delete_complete").wait(StackName=stack + "-agent")
        print("the agent's role and security group are removed")
    except Exception as error:      # the network interfaces AgentCore made can take a while to go
        print("the agent's stack is not gone yet:", str(error)[:160])
    path = os.path.join(HERE, f".runtime-{stack}")
    if os.path.exists(path):
        os.remove(path)


def main(argv):
    if len(argv) < 2 or argv[0] not in ("up", "ask", "remove") or not NAME.fullmatch(argv[1]) or (argv[0] == "ask" and len(argv) < 3):
        sys.exit(__doc__)
    import boto3
    session = boto3.Session(profile_name=PROFILE or None, region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        sys.exit("STOP: wrong account")
    {"up": lambda: up(session, argv[1]), "ask": lambda: ask(session, argv[1], argv[2]), "remove": lambda: remove(session, argv[1])}[argv[0]]()


if __name__ == "__main__":
    main(sys.argv[1:])

"""The second way through AgentCore: a HARNESS whose model is the sealed model. No agent code, only settings.

    python consume/harness.py up <customer stack>        about 25 minutes the first time
    python consume/harness.py ask <customer stack> "<question>"
    python consume/harness.py remove <customer stack>

`up` makes, in the customer's own account and network (stack <name>-harness): a private API Gateway in front of
the sealed endpoint (a harness trusts only public certificates; the sealed machine's is self-signed), the private
doors a harness needs to start, a role and a security group. It then stores the customer's API key in AgentCore
Identity and creates the harness with  model = the sealed endpoint  and  network = this private network.
Run on 28 Nov (docs/m13-plan.md, C6). Not wired into smh.sh.
shortcut: the gateway does not check who signed the sealed machine's certificate; give the endpoint a public certificate
and point apiBase straight at it when a real domain exists.
"""
import json
import os
import re
import sys
import time
import uuid

ACCOUNT, REGION, PROFILE = (os.environ.get("SMH_ACCOUNT", "111122223333"), os.environ.get("SMH_REGION", "us-east-1"),
                            os.environ.get("SMH_PROFILE", "poc-ino"))
OWNER = os.environ.get("SMH_OWNER", "owner@example.com")
TAGS = {"business-unit": "ai", "env": "innovation", "category": "rnd", "owner": OWNER, "app": "Secure-model-hosting", "expire-date": "recurring"}
NAME = re.compile(r"smh-poc-oc[a-z0-9-]{0,12}")
HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT = "You are a helpful assistant running on a sealed model inside the customer's own AWS account. Answer briefly."


def names(stack):
    short = re.sub(r"[^a-z0-9]", "_", stack[len("smh-poc-"):])
    return "smh_poc_sealed_" + short, "smh-poc-sealed-key-" + stack[len("smh-poc-"):]       # harness, key provider


def outputs(cf, stack):
    return {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])}


def find(control, name):
    for h in control.list_harnesses().get("harnesses", []):
        if h["harnessName"] == name:
            return h
    return None


def up(session, stack):
    cf, ec2, s3, elb = (session.client(n) for n in ("cloudformation", "ec2", "s3", "elbv2"))
    control = session.client("bedrock-agentcore-control")
    o = outputs(cf, stack)
    host = o["Endpoint"].split("://", 1)[1]
    balancer = next(b for b in elb.describe_load_balancers()["LoadBalancers"] if b["DNSName"] == host)
    cidr = ec2.describe_vpcs(VpcIds=[o["VpcId"]])["Vpcs"][0]["CidrBlock"]
    with open(os.path.join(HERE, "..", "infra", "oneclick", "consume-harness.yaml")) as f:
        template = f.read()
    logs_sg = ec2.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [o["VpcId"]]}, {"Name": "service-name", "Values": [f"com.amazonaws.{REGION}.logs"]}])["VpcEndpoints"][0]["Groups"][0]["GroupId"]
    address = ec2.describe_instances(InstanceIds=[o["InstanceId"]])["Reservations"][0]["Instances"][0]["PrivateIpAddress"]
    params = {"Name": stack, "VpcId": o["VpcId"], "SubnetId": o["SubnetId"], "VpcCidr": cidr, "BalancerArn": balancer["LoadBalancerArn"], "BalancerDns": host,
              "LogsEndpointSg": logs_sg, "CertificateHost": address}
    kwargs = dict(StackName=stack + "-harness", TemplateBody=template, Capabilities=["CAPABILITY_NAMED_IAM"],
                  Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()], Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()])
    try:
        cf.create_stack(**kwargs)
        print("making the private gateway and the harness's doors (about 6 minutes)")
        cf.get_waiter("stack_create_complete").wait(StackName=stack + "-harness", WaiterConfig={"Delay": 15, "MaxAttempts": 80})
    except cf.exceptions.AlreadyExistsException:
        pass
    h = outputs(cf, stack + "-harness")
    # CloudFormation cannot set this one: the gateway must not expect a public certificate from the sealed machine.
    # (See the template's note: this hop stays inside the customer's private network.)
    gw = session.client("apigateway")
    api_id = h["ApiBase"].split("//")[1].split(".")[0]
    resource = next(r["id"] for r in gw.get_resources(restApiId=api_id)["items"] if r.get("pathPart") == "{proxy+}")
    gw.update_integration(restApiId=api_id, resourceId=resource, httpMethod="ANY",
                          patchOperations=[{"op": "replace", "path": "/tlsConfig/insecureSkipVerification", "value": "true"}])
    gw.create_deployment(restApiId=api_id, stageName="sealed")
    # Before waiting 20 minutes for a harness: does the gateway reach the sealed machine at all?
    for _ in range(20):                 # a new private link needs a few minutes before the gateway can use it
        probe = gw.test_invoke_method(restApiId=api_id, resourceId=resource, httpMethod="GET", pathWithQueryString="/health")
        if probe["status"] == 200:
            break
        time.sleep(30)
    if probe["status"] != 200:
        why = [line for line in probe.get("log", "").splitlines() if "failed" in line.lower() or "error" in line.lower()]
        sys.exit(f"the private gateway cannot reach the sealed machine (status {probe['status']}): {(why or ['no reason given'])[-1][-260:]}")
    print("the private gateway reaches the sealed machine (health 200)")
    harness_name, provider = names(stack)
    # The customer's API key goes into AgentCore Identity. It is read from the customer's bucket and never printed.
    key = s3.get_object(Bucket=o["Bucket"], Key="gate/api-keys.txt")["Body"].read().decode().split()[0]
    try:
        arn = control.create_api_key_credential_provider(name=provider, apiKey=key, tags=TAGS)["credentialProviderArn"]
    except Exception as error:
        if "exist" not in str(error).lower():
            raise
        control.update_api_key_credential_provider(name=provider, apiKey=key)
        arn = control.get_api_key_credential_provider(name=provider)["credentialProviderArn"]
    del key
    spec = dict(
        executionRoleArn=h["RoleArn"],
        model={"openAiModelConfig": {"modelId": os.environ.get("SMH_MODEL", "demo"), "apiKeyArn": arn, "apiBase": h["ApiBase"], "maxTokens": 400,
                                     "apiFormat": "chat_completions"}},      # the sealed machine's gate allows the chat address, not "responses"
        systemPrompt=[{"text": PROMPT}],
        environment={"agentCoreRuntimeEnvironment": {"networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
            "subnets": [o["SubnetId"]], "securityGroups": [h["SecurityGroup"]]}}}},
        maxIterations=6, maxTokens=400, timeoutSeconds=120)
    existing = find(control, harness_name)
    if existing:
        control.update_harness(harnessId=existing["harnessId"], memory={"optionalValue": {"disabled": {}}}, **spec)
        hid = existing["harnessId"]
    else:
        hid = control.create_harness(harnessName=harness_name, memory={"disabled": {}}, tags=TAGS, **spec)["harness"]["harnessId"]
    for _ in range(180):                # a harness in a private network took about 20 minutes to start (28 Nov)
        got = control.get_harness(harnessId=hid)["harness"]
        if got["status"] == "READY":
            break
        if got["status"].endswith("FAILED"):
            sys.exit(f"harness {got['status']}: {json.dumps(got.get('failureReason', ''))[:300]}")
        time.sleep(10)
    else:
        sys.exit("the harness did not become ready in 30 minutes")
    with open(os.path.join(HERE, f".harness-{stack}"), "w") as f:
        f.write(got["arn"])
    print(f"harness ready: {harness_name}; its model is the sealed model at {h['ApiBase']}")


def ask(session, stack, prompt):
    with open(os.path.join(HERE, f".harness-{stack}")) as f:
        arn = f.read().strip()
    from botocore.config import Config
    stream = session.client("bedrock-agentcore", config=Config(read_timeout=420, retries={"max_attempts": 1})).invoke_harness(
        harnessArn=arn, runtimeSessionId=str(uuid.uuid4()), messages=[{"role": "user", "content": [{"text": prompt}]}])["stream"]
    text = ""
    for event in stream:
        if "contentBlockDelta" in event:
            text += event["contentBlockDelta"].get("delta", {}).get("text", "")
        elif "messageStop" in event and event["messageStop"].get("stopReason") not in (None, "end_turn", "tool_use", "tool_result"):
            text += f" [stopped: {event['messageStop'].get('stopReason')}]"
        elif any(k.endswith("Exception") or k.endswith("Error") for k in event):
            sys.exit("The harness says: " + json.dumps(event, default=str)[:400])
    print(text.strip() or "(no text came back)")
    print("  (the sealed model, through an AgentCore harness)")


def remove(session, stack):
    cf, control = session.client("cloudformation"), session.client("bedrock-agentcore-control")
    harness_name, provider = names(stack)
    existing = find(control, harness_name)
    if existing:
        control.delete_harness(harnessId=existing["harnessId"])
        for _ in range(60):
            if not find(control, harness_name):
                break
            time.sleep(10)
        print("harness removed")
    try:
        control.delete_api_key_credential_provider(name=provider)
    except Exception:
        pass
    for attempt in (1, 2):              # the first try fails: the gateway's deployed copy still names the private link (28 Nov)
        try:
            cf.delete_stack(StackName=stack + "-harness")
            cf.get_waiter("stack_delete_complete").wait(StackName=stack + "-harness", WaiterConfig={"Delay": 15, "MaxAttempts": 80})
            print("the private gateway and the harness's doors are removed")
            break
        except Exception as error:
            if attempt == 2:
                sys.exit("the harness stack is not gone: " + str(error)[:160])
    path = os.path.join(HERE, f".harness-{stack}")
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

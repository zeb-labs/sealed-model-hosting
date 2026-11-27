"""The Lambda function behind the AgentCore Gateway: real AWS wiring for tools.py.

Its role can: read and write deploy/ in the builder's bucket, read build
results and approvals (never write approvals), start ONE Systems Manager
document on our two machines, start and stop the builder, create and delete
the GPU helper stack. It cannot decrypt, cannot read model objects, and
cannot change a key policy.
"""
import json
import os
import urllib.request

import boto3

import tools

BUCKET = os.environ["BUCKET"]
DOCUMENT = os.environ["DOCUMENT"]
HELPER_STACK, BUILDER_STACK = "smh-poc-m1-gpu-helper", "smh-poc-m1-builder"
TAGS = [{"Key": k, "Value": v} for k, v in json.loads(os.environ["TAGS"]).items()]
s3, ssm, ec2, cfn = (boto3.client(n) for n in ("s3", "ssm", "ec2", "cloudformation"))


class Real(tools.Cloud):
    def get(self, key):
        try:
            return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        except s3.exceptions.NoSuchKey:
            return None

    def put(self, key, body):
        if not key.startswith("deploy/"):          # the role says the same; this is the second lock
            raise tools.Refused("the tools write under deploy/ only")
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)

    def _stack(self, name):
        try:
            return cfn.describe_stacks(StackName=name)["Stacks"][0]
        except cfn.exceptions.ClientError:
            return None

    def _instance(self, which):
        stack = self._stack(BUILDER_STACK if which == "builder" else HELPER_STACK)
        if not stack:
            return None
        return next((o["OutputValue"] for o in stack.get("Outputs", []) if o["OutputKey"] == "InstanceId"), None)

    def _ssm_online(self, instance):
        info = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance]}])["InstanceInformationList"]
        return bool(info) and info[0]["PingStatus"] == "Online"

    def machine_state(self, which):
        if which == "helper":
            stack = self._stack(HELPER_STACK)
            if not stack:
                return "absent"
            status = stack["StackStatus"]
            if status in ("CREATE_IN_PROGRESS",):
                return "starting"
            if status != "CREATE_COMPLETE":
                events = cfn.describe_stack_events(StackName=HELPER_STACK)["StackEvents"]
                reasons = " ".join(e.get("ResourceStatusReason", "") for e in events if e["ResourceStatus"] == "CREATE_FAILED")
                return "failed:capacity" if "capacity" in reasons.lower() else "failed:other"
        instance = self._instance(which)
        if not instance:
            return "absent"
        state = ec2.describe_instances(InstanceIds=[instance])["Reservations"][0]["Instances"][0]["State"]["Name"]
        if state == "running":
            return "online" if self._ssm_online(instance) else "starting"
        return "starting" if state == "pending" else "stopped"

    def machine_up(self, which, gpu):
        if which == "builder":
            ec2.start_instances(InstanceIds=[self._instance("builder")])
            return
        stack = self._stack(HELPER_STACK)
        if stack and stack["StackStatus"] in ("CREATE_COMPLETE", "CREATE_IN_PROGRESS"):
            return
        if stack:                                   # a failed attempt: clear it, the agent asks again
            cfn.delete_stack(StackName=HELPER_STACK, RoleARN=os.environ["CFN_ROLE"])
            return
        cfn.create_stack(StackName=HELPER_STACK, RoleARN=os.environ["CFN_ROLE"], Tags=TAGS,
                         TemplateURL=f"https://{BUCKET}.s3.amazonaws.com/build/src/infra/m1/gpu-helper.yaml",
                         Parameters=[{"ParameterKey": "InstanceType", "ParameterValue": gpu}])

    def machine_down(self, which):
        if which == "builder":
            ec2.stop_instances(InstanceIds=[self._instance("builder")])
        elif self._stack(HELPER_STACK):
            cfn.delete_stack(StackName=HELPER_STACK, RoleARN=os.environ["CFN_ROLE"])

    def send_step(self, machine, step, version, prefix, model, gpu):
        return ssm.send_command(
            InstanceIds=[self._instance(machine)], DocumentName=DOCUMENT, TimeoutSeconds=120,
            Parameters={"Step": [step], "Version": [version], "Prefix": [prefix], "Model": [model], "Gpu": [gpu]},
        )["Command"]["CommandId"]

    def command_state(self, machine, command_id):
        try:
            status = ssm.get_command_invocation(CommandId=command_id, InstanceId=self._instance(machine))["Status"]
        except Exception:
            return "failed"                          # the machine is gone, or the command expired
        return {"Pending": "running", "InProgress": "running", "Delayed": "running", "Success": "done"}.get(status, "failed")

    def stack_state(self, name):
        stack = self._stack(name)
        if not stack:
            return "absent"
        return "ready" if stack["StackStatus"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE") else stack["StackStatus"]

    def model_info(self, model):
        if model.startswith("local/"):        # the builder's own weights, staged by `smh.sh ship <folder>`; no Hugging Face involved
            body = self.get(f"deploy/models-in/{model[len('local/'):]}/model-info.json")
            if body is None:
                raise tools.Refused("no staged model of that name: run  bash smh.sh ship <folder with your model>")
            return json.loads(body)
        with urllib.request.urlopen(f"https://huggingface.co/api/models/{model}?blobs=true", timeout=20) as r:
            return json.loads(r.read())


def lambda_handler(event, context):
    # The Gateway puts the tool's name in the client context as "<target>___<tool>".
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = str(custom.get("bedrockAgentCoreToolName") or event.pop("__tool", "")).split("___")[-1]
    result = tools.call(Real(), name, event if isinstance(event, dict) else {})
    print(json.dumps({"tool": name, "inputs": event, "refused": result.get("refused"), "state": result.get("state")}))
    return result

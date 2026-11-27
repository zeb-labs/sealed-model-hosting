"""Fetch evidence from the account into a run folder. Read-only: every call
here lists, describes or downloads. `assure.sh` does the launching.

    python3 assure/collect.py static <run> <ami> <build-dir>
    python3 assure/collect.py live   <run> <sealed-instance> <client-instance> <start-ms>
    python3 assure/collect.py trail  <dir> <start-iso>         # CloudTrail for both keys
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List

import os as _os

# On the laptop: the poc-ino profile. On the builder (SMH_PROFILE set to nothing): its own role.
PROFILE, REGION, ACCOUNT = _os.environ.get("SMH_PROFILE", "poc-ino"), _os.environ.get("SMH_REGION", "us-east-1"), _os.environ.get("SMH_ACCOUNT", "111122223333")
ARTIFACTS, CUSTOMER = f"smh-poc-artifacts-{ACCOUNT}", f"smh-poc-customer-{ACCOUNT}"
CUSTOMER_VPC_NAME = "smh-poc-customer-vpc"


def aws(*args: str, text: bool = False) -> Any:
    out = subprocess.run(["aws", *(["--profile", PROFILE] if PROFILE else []), "--region", REGION, "--no-cli-pager", *args,
                          "--output", "text" if text else "json"], check=True, capture_output=True).stdout.decode()
    return out.strip() if text else (json.loads(out) if out.strip() else None)


def save(run: str, name: str, value: Any) -> None:
    path = os.path.join(run, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value if isinstance(value, str) else json.dumps(value, indent=1, default=str))


def attempt(label: str, fn) -> None:
    """One missing piece must not stop the rest; the report will say NOT RUN for it."""
    try:
        fn()
    except Exception as error:
        detail = error.stderr.decode()[-200:] if isinstance(error, subprocess.CalledProcessError) else str(error)[:200]
        print(f"  could not collect {label}: {type(error).__name__} {detail.strip()}")


def role(name: str) -> Dict[str, Any]:
    statements: List[Any] = []
    for policy in aws("iam", "list-role-policies", "--role-name", name)["PolicyNames"]:
        doc = aws("iam", "get-role-policy", "--role-name", name, "--policy-name", policy)["PolicyDocument"]
        st = doc["Statement"]
        statements += st if isinstance(st, list) else [st]
    attached = [p["PolicyName"] for p in aws("iam", "list-attached-role-policies", "--role-name", name)["AttachedPolicies"]]
    return {"statements": statements, "attached": attached}


def customer_vpc() -> str:
    return aws("ec2", "describe-vpcs", "--filters", f"Name=tag:Name,Values={CUSTOMER_VPC_NAME}",
               "Name=tag:app,Values=Secure-model-hosting", "--query", "Vpcs[0].VpcId", text=True)


def key_policy(alias: str) -> Any:
    key_id = aws("kms", "describe-key", "--key-id", alias, "--query", "KeyMetadata.KeyId", text=True)
    return json.loads(aws("kms", "get-key-policy", "--key-id", key_id, "--policy-name", "default", "--query", "Policy", text=True))


def static(run: str, ami: str, build: str) -> None:
    for name in ("summary.json", "pipeline.log", "repro.json"):
        if os.path.exists(os.path.join(build, name)):
            os.makedirs(os.path.join(run, "static"), exist_ok=True)
            shutil.copy(os.path.join(build, name), os.path.join(run, "static", name))
    version = json.load(open(os.path.join(build, "summary.json")))["version"]

    attempt("model key policy", lambda: save(run, "static/model-key-policy.json", key_policy("alias/smh-poc-model-key")))
    attempt("lease key policy", lambda: save(run, "static/lease-key-policy.json", key_policy("alias/smh-poc-lease-key")))
    attempt("image", lambda: save(run, "static/image.json", aws("ec2", "describe-images", "--image-ids", ami)["Images"][0]))
    attempt("Secure Boot data", lambda: save(run, "static/uefi-data.txt", aws(
        "ec2", "describe-image-attribute", "--image-id", ami, "--attribute", "uefiData", "--query", "UefiData.Value", text=True)))

    def packages() -> None:
        listing = aws("s3api", "list-objects-v2", "--bucket", ARTIFACTS, "--prefix", f"build/out-{version}/")
        key = next(o["Key"] for o in listing["Contents"] if o["Key"].endswith(".packages"))
        aws("s3", "cp", "--only-show-errors", f"s3://{ARTIFACTS}/{key}", os.path.join(run, "static", "packages.txt"), text=True)

    attempt("package list", packages)
    attempt("sealed role", lambda: save(run, "static/sealed-role.json", role("smh-poc-sealed-instance-role")))
    attempt("builder role", lambda: save(run, "static/builder-role.json", role("smh-poc-builder-role")))

    def deployer() -> None:
        names = [r["RoleName"] for r in aws("iam", "list-roles", "--path-prefix", "/zl/")["Roles"] if r["RoleName"].startswith("smh-poc-deployer-")]
        save(run, "static/deployer-roles.json", {n: role(n) for n in names})

    attempt("deployer roles", deployer)

    def network() -> None:
        vpc = customer_vpc()
        f = f"Name=vpc-id,Values={vpc}"
        groups = aws("ec2", "describe-security-groups", "--filters", f)["SecurityGroups"]
        by_name = {g["GroupName"]: g for g in groups}
        save(run, "static/network.json", {
            "igws": aws("ec2", "describe-internet-gateways", "--filters", f"Name=attachment.vpc-id,Values={vpc}")["InternetGateways"],
            "nats": aws("ec2", "describe-nat-gateways", "--filter", f)["NatGateways"],
            "peerings": [p for side in ("requester", "accepter") for p in aws(
                "ec2", "describe-vpc-peering-connections", "--filters", f"Name={side}-vpc-info.vpc-id,Values={vpc}")["VpcPeeringConnections"]],
            "endpoints": [e["ServiceName"] for e in aws("ec2", "describe-vpc-endpoints", "--filters", f)["VpcEndpoints"]
                          if e["State"] not in ("deleted", "deleting")],
            "sealed_ingress": by_name["smh-poc-sealed-sg"]["IpPermissions"],
            "client_sg": by_name.get("smh-poc-client-sg", {}).get("GroupId"),
        })

    attempt("network", network)

    def bucket() -> None:
        heads = {}
        # Every locked object in the bucket, whatever folder a release put it in.
        # (Until 22 Nov this looked in three fixed folders and so missed the agent's new one.)
        for prefix in ("",):
            for obj in (aws("s3api", "list-objects-v2", "--bucket", ARTIFACTS, "--query", "{Contents: Contents[?ends_with(Key, `.sbox`)]}") or {}).get("Contents") or []:
                if obj["Key"].endswith(".sbox"):
                    tmp = os.path.join(run, "static", "head.bin")
                    aws("s3api", "get-object", "--bucket", ARTIFACTS, "--key", obj["Key"], "--range", "bytes=0-5", tmp)
                    heads[obj["Key"]] = open(tmp, "rb").read().hex()
                    os.remove(tmp)
        block = aws("s3api", "get-public-access-block", "--bucket", ARTIFACTS)["PublicAccessBlockConfiguration"]
        save(run, "static/bucket.json", {"heads": heads, "public_block": block})

    attempt("bucket objects", bucket)


def live(run: str, sealed: str, client: str, start_ms: str) -> None:
    def addr(instance: str) -> str:
        return aws("ec2", "describe-instances", "--instance-ids", instance,
                   "--query", "Reservations[0].Instances[0].PrivateIpAddress", text=True)

    def ids() -> None:
        vpc = customer_vpc()
        enis = aws("ec2", "describe-network-interfaces", "--filters", f"Name=vpc-id,Values={vpc}")["NetworkInterfaces"]
        known = {}
        try:
            known = json.load(open(os.path.join(run, "live", "ids.json")))
        except Exception:
            pass
        known.update({
            "instance": sealed, "sealed_ip": addr(sealed), "client_ip": addr(client),
            "sealed_eni": aws("ec2", "describe-instances", "--instance-ids", sealed, "--query",
                              "Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId", text=True),
            "endpoint_ips": [e["PrivateIpAddress"] for e in enis if e["InterfaceType"] == "vpc_endpoint"],
            "s3_cidrs": aws("ec2", "describe-prefix-lists", "--filters", f"Name=prefix-list-name,Values=com.amazonaws.{REGION}.s3")["PrefixLists"][0]["Cidrs"],
        })
        save(run, "live/ids.json", known)

    attempt("addresses", ids)
    attempt("Systems Manager list", lambda: save(run, "live/ssm.json", [
        i["InstanceId"] for i in aws("ssm", "describe-instance-information")["InstanceInformationList"]]))
    attempt("customer log", lambda: save(run, "live/customer-log.json", aws(
        "logs", "filter-log-events", "--log-group-name", "/smh-poc/customer/sealed-machine", "--start-time", start_ms)))
    attempt("flow logs", lambda: save(run, "live/flow-logs.json", aws(
        "logs", "filter-log-events", "--log-group-name", "/smh-poc/customer/vpc-flow", "--start-time", start_ms)))


def trail(folder: str, start_iso: str) -> None:
    for name, alias in (("model", "alias/smh-poc-model-key"), ("lease", "alias/smh-poc-lease-key")):
        arn = aws("kms", "describe-key", "--key-id", alias, "--query", "KeyMetadata.Arn", text=True)
        attempt(f"CloudTrail {name} key", lambda: save(folder, f"cloudtrail-{name}.json", aws(
            "cloudtrail", "lookup-events", "--lookup-attributes", f"AttributeKey=ResourceName,AttributeValue={arn}",
            "--start-time", start_iso)))


if __name__ == "__main__":
    if aws("sts", "get-caller-identity", "--query", "Account", text=True) != ACCOUNT:
        raise SystemExit("STOP: wrong account")
    mode, args = sys.argv[1], sys.argv[2:]
    {"static": static, "live": live, "trail": trail}[mode](*args)

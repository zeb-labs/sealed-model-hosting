"""The builder's books: which customers may use the model, and which releases exist.

    python deployer/manage.py customers [--json]            list
    python deployer/manage.py customer add <name> <their 12-digit AWS account | machine role ARN>
    python deployer/manage.py customer pause|resume|remove <name>
    python deployer/manage.py key-params                     the two values the key stacks take (used by smh.sh)
    python deployer/manage.py models [--json]                releases, and which one is live

A customer is a name and the ARN of the role their sealed machine runs as (it may be in another AWS account).
Give only their account number and the role is the one their stack will make: the stack is named
smh-poc-oc-<name> and its machine role role/zl/smh-poc-oc-<name>-sealed-role. So a customer can be registered,
and the release shared with them, BEFORE they install anything.
The registry is one small file in the builder's bucket: deploy/customers/registry.json. Only ACTIVE customers are
written into the two key policies, each under the same measurements; pausing one removes only that one, and its
machine stops within about four minutes because its lease can no longer be renewed. The others never notice.

shortcut: the active roles travel as one CloudFormation parameter (4096 characters, about 45 customers); beyond
that, write the key policy from the registry directly instead of through a parameter.
Offline tests: deployer/test_manage.py.
"""
import json
import os
import re
import sys
import time

ACCOUNT, REGION, PROFILE = (os.environ.get("SMH_ACCOUNT", "111122223333"), os.environ.get("SMH_REGION", "us-east-1"),
                            os.environ.get("SMH_PROFILE", "poc-ino"))
BUCKET, KEY = f"smh-poc-artifacts-{ACCOUNT}", "deploy/customers/registry.json"
NAME = re.compile(r"[a-z][a-z0-9-]{1,30}")
SHORT = re.compile(r"[a-z][a-z0-9]{1,10}")          # a name that also fits into the customer's stack name
ROLE = re.compile(r"arn:aws:iam::(\d{12}):role/([\w+=,.@-]+/)*[\w+=,.@-]{1,64}")
MAX_PARAMETER = 4096


class Refused(Exception):
    pass


def _one(reg, name):
    if name not in reg["customers"]:
        raise Refused(f"there is no customer named {name}")
    return reg["customers"][name]


def stack_of(name):
    """The customer's own stack, and so every name inside it. The first test customer keeps the bare name."""
    return name if name.startswith("smh-poc-oc") else f"smh-poc-oc-{name}"


def add(reg, name, role, now=None):
    if not NAME.fullmatch(name or ""):
        raise Refused("a customer's name is 2 to 31 lower-case letters, digits or dashes, starting with a letter")
    if re.fullmatch(r"\d{12}", role or ""):           # only their account number: the role their stack will make
        if not SHORT.fullmatch(name):
            raise Refused("to register by account number, the name must be 2 to 11 lower-case letters or digits (it becomes part of their stack's name)")
        role = f"arn:aws:iam::{role}:role/zl/{stack_of(name)}-sealed-role"
    if not ROLE.fullmatch(role or ""):
        raise Refused("the machine role must be a full role ARN, like arn:aws:iam::111122223333:role/zl/name-sealed-role")
    for other, c in reg["customers"].items():
        if c["role"] == role and other != name:
            raise Refused(f"that role already belongs to customer {other}")
    old = reg["customers"].get(name, {})
    reg["customers"][name] = {"role": role, "state": "active", "added": old.get("added") or time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))}
    key_params(reg)                                     # refuse here, not half-way through a stack update
    return reg


def pause(reg, name):
    _one(reg, name)["state"] = "paused"
    return reg


def resume(reg, name):
    _one(reg, name)["state"] = "active"
    return reg


def remove(reg, name):
    _one(reg, name)
    del reg["customers"][name]
    return reg


def key_params(reg):
    """(roles, account roots) of the ACTIVE customers, comma separated: the two parameters of infra/m1/key.yaml."""
    roles = sorted(c["role"] for c in reg["customers"].values() if c["state"] == "active")
    roots = sorted({f"arn:aws:iam::{ROLE.fullmatch(r).group(1)}:root" for r in roles})
    joined = ",".join(roles)
    if len(joined) > MAX_PARAMETER:
        raise Refused("too many active customers for one CloudFormation parameter (see the note at the top of deployer/manage.py)")
    return joined, ",".join(roots)


def rows(reg):
    return [{"name": n, "account": ROLE.fullmatch(c["role"]).group(1), "role": c["role"], "state": c["state"], "added": c["added"], "stack": stack_of(n)}
            for n, c in sorted(reg["customers"].items())]


def releases(states, approvals, images, live_pcr4, summaries):
    """One row per release. `states`: version -> deploy state; `images`: version -> ami or None; `summaries`: version -> PCR4."""
    out = []
    for version in sorted(states, key=lambda v: [int(x) for x in v.split(".")], reverse=True):
        if version == "0.0.0":
            continue
        s, pcr4 = states[version], summaries.get(version)
        out.append({"version": version, "model": s.get("model", "?"), "folder": s.get("prefix", "?"), "machine": s.get("gpu", "?"),
                    "image": images.get(version) or "deleted", "approved": version in approvals,
                    "state": "live" if pcr4 and pcr4 == live_pcr4 and images.get(version) else "not usable: image deleted" if not images.get(version)
                    else "available" if version in approvals else "prepared, not approved"})
    return out


# ------------------------------------------------------------------ the real wiring

def _session():
    import boto3
    s = boto3.Session(profile_name=PROFILE or None, region_name=REGION)
    if s.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        sys.exit("STOP: wrong account")
    return s


def load(s3, cf):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=KEY)["Body"].read())
    except s3.exceptions.NoSuchKey:
        # First use: whoever the key policy already names becomes the first customer in the books.
        reg = {"customers": {}}
        p = {x["ParameterKey"]: x.get("ParameterValue", "") for x in cf.describe_stacks(StackName="smh-poc-m1-key")["Stacks"][0]["Parameters"]}
        for role in filter(None, p.get("CustomerRoleArn", "").split(",")):
            if ROLE.fullmatch(role):
                add(reg, re.sub(r"-sealed-role$", "", role.split("/")[-1])[:31], role)
        return reg


def save(s3, reg):
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps(reg, indent=1).encode())


def _models(session):
    s3, ec2, cf = session.client("s3"), session.client("ec2"), session.client("cloudformation")

    def get(key):
        try:
            return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        except Exception:
            return None
    versions = [p["Prefix"].split("/")[1] for p in s3.list_objects_v2(Bucket=BUCKET, Prefix="deploy/", Delimiter="/").get("CommonPrefixes", [])
                if re.fullmatch(r"\d+\.\d+\.\d+", p["Prefix"].split("/")[1])]
    states, approvals, amis, summaries = {}, set(), {}, {}
    for v in versions:
        raw = get(f"deploy/{v}/state.json")
        if not raw:
            continue
        states[v] = json.loads(raw)
        if get(f"approvals/{v}.json"):
            approvals.add(v)
        ami = (get(f"build/out-{v}/ami-id.txt") or b"").decode().split()
        amis[v] = ami[0] if ami else None
        summary = get(f"build/out-{v}/summary.json")
        summaries[v] = json.loads(summary).get("PCR4") if summary else None
    wanted = [a for a in amis.values() if a]
    existing = {i["ImageId"] for i in ec2.describe_images(Owners=["self"], ImageIds=wanted)["Images"]} if wanted else set()
    images = {v: (a if a in existing else None) for v, a in amis.items()}
    live = {x["ParameterKey"]: x.get("ParameterValue", "") for x in cf.describe_stacks(StackName="smh-poc-m1-key")["Stacks"][0]["Parameters"]}.get("Pcr4", "")
    return releases(states, approvals, images, live, summaries)


def _table(items, columns):
    if not items:
        return "  (none)"
    width = {c: max(len(c), *(len(str(i[c])) for i in items)) for c in columns}
    lines = ["  " + "  ".join(c.upper().ljust(width[c]) for c in columns)]
    lines += ["  " + "  ".join(str(i[c]).ljust(width[c]) for c in columns) for i in items]
    return "\n".join(lines)


def main(argv):
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv:
        sys.exit(__doc__)
    session = _session()
    s3, cf = session.client("s3"), session.client("cloudformation")
    try:
        if argv[0] == "models":
            items = _models(session)
            print(json.dumps(items) if as_json else _table(items, ["version", "model", "state", "image", "machine"]))
            return
        reg = load(s3, cf)
        if argv[0] == "customer":
            action, name = argv[1], argv[2]
            reg = {"add": lambda: add(reg, name, argv[3] if len(argv) > 3 else ""), "pause": lambda: pause(reg, name),
                   "resume": lambda: resume(reg, name), "remove": lambda: remove(reg, name)}[action]()
            save(s3, reg)
            print(f"customer {name}: {action} recorded")
        elif argv[0] == "key-params":
            save(s3, reg)                               # the first load may have just made it
            print("\n".join(key_params(reg)))
        elif argv[0] == "customers":
            save(s3, reg)
            print(json.dumps(rows(reg)) if as_json else _table(rows(reg), ["name", "state", "account", "added", "role"]))
        else:
            sys.exit(__doc__)
    except Refused as error:
        sys.exit(f"Refused: {error}")
    except (KeyError, IndexError):
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

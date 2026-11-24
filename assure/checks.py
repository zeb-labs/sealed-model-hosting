"""The assurance suite's judges: raw evidence in, PASS or FAIL out.

Nothing here talks to AWS. `assure.sh` collects the evidence (console text,
CloudTrail, policies, flow logs) into files; each function below reads one
kind and says what it proves. That keeps the judging testable offline against
the evidence saved in M1 to M5 (assure/test_checks.py).

A check that finds nothing wrong returns PASS with a one-line "seen". A check
whose evidence is missing is reported as NOT RUN by report.py, never as PASS.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

# id -> (property, what must be true). The report lists every id, run or not.
TESTS: Dict[str, tuple] = {
    "S1": ("all", "All offline unit tests pass"),
    "S2": ("P1", "Model key: no account-wide statement; released only to the sealed role with PCR4, 7, 12 and the OPEN runtime value of this build"),
    "S3": ("P6", "Lease key: same, with the LOCKED runtime value of this build"),
    "S4": ("P1", "Image registered with Secure Boot data and TPM; not public"),
    "S5": ("P2", "No remote-login package in the image; the build check passed"),
    "S6": ("P4", "Sealed machine's role: only the allowed actions on the allowed places; no KMS right; no wildcard"),
    "S7": ("P2, P4", "Customer network: no internet gateway, NAT or peering; endpoints S3, KMS, Logs only; no inbound except the gate from the client group"),
    "S8": ("P3", "Every model and container object in the builder's bucket is in our encrypted format; the bucket blocks public access"),
    "S9": ("P4", "Builder's role can read usage/ on the customer's side and nothing else"),
    "S11": ("P1", "Deployer agent's roles: no KMS right, no write to approvals, no way to run anything but the one fixed document"),
    "S10": ("Q7", "Building the same recipe twice gives the same boot measurement (known open item)"),
    "L1": ("P1", "Good boot: key released once, door locked, runtime value as predicted, model answers"),
    "L2": ("P1", "Hostile launch inputs (user data, tags) change no measured value and run nothing"),
    "L3": ("P2", "No login prompt; not reachable by Systems Manager; port scan finds only the gate"),
    "L4": ("P1", "Second key request after the lock is refused"),
    "L5": ("P2", "Front gate: four paths work; every other path is 'not found'; per-token probabilities refused"),
    "L6": ("P5", "Good job form trains; each bad form is refused with a reason"),
    "L7": ("P5", "A job leaves only adapter, adapter settings and metrics, within the size limit"),
    "L8": ("P4", "Customer's log has the machine's lines from the first boot line on, and no trainer lines"),
    "L9": ("P4", "Network: outbound only to S3, KMS and Logs; inbound only from the client to the gate"),
    "L10": ("P4", "Usage records: seals pass; a changed and a deleted record are caught; builder cannot list other folders"),
    "L11": ("P1, P6", "CloudTrail: model key released once per boot; lease renewed in between"),
    "L12": ("P3", "After the run the system disk is byte-for-byte the released image"),
    "L13": ("P6", "Keys disabled: the machine stops answering within the bound and powers off"),
    "B1": ("P1", "Registered without Secure Boot data: no key"),
    "B2": ("P1", "Added boot-time file: no key"),
    "B3": ("P1", "One changed block on the system disk: does not come up"),
    "B4": ("P1", "Different instance role: no key"),
    "B5": ("P1", "Swapped container archive: loader stops before asking for the key"),
    "B6": ("P3", "One changed byte in the encrypted weights: the model never serves"),
    "B7": ("P2", "Customer adds a Systems Manager role and endpoints: the machine does not register, and gets no key"),
    "B8": ("P3", "Hibernation asked for and requested: no memory image is written to the disk"),
    "R1": ("P1", "After the break tier the untouched image boots and serves again (the two swapped objects are back)"),
}


@dataclass
class Result:
    id: str
    verdict: str            # PASS, FAIL or NOT RUN
    seen: str
    evidence: str = ""

    @property
    def prop(self) -> str:
        return TESTS[self.id][0]

    @property
    def expected(self) -> str:
        return TESTS[self.id][1]


def _result(test_id: str, problems: Sequence[str], good: str, evidence: str = "") -> Result:
    if problems:
        return Result(test_id, "FAIL", "; ".join(problems)[:600], evidence)
    return Result(test_id, "PASS", good, evidence)


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------- static

_PCR_KEYS = {f"kms:recipientattestation:nitrotpmpcr{n}": n for n in (4, 7, 12, 15)}


def key_policy(test_id: str, policy: Dict[str, Any], sealed_role_arn: str, pcrs: Dict[int, str]) -> Result:
    """S2, S3. `pcrs` maps 4, 7, 12, 15 to the values this build must be pinned to."""
    problems, releases, customers = [], 0, []
    for st in _list(policy.get("Statement", [])):
        if st.get("Effect") != "Allow":
            continue
        principals = [str(p) for p in _list(st["Principal"].get("AWS", [])) ] if isinstance(st.get("Principal"), dict) else [str(st.get("Principal"))]
        actions = [str(a).lower() for a in _list(st.get("Action", []))]
        sid = st.get("Sid", "?")
        # M10: one onboarded customer role. The account is the principal, but a
        # condition narrows it to exactly one role, and the PCR conditions below still apply.
        named = st.get("Condition", {}).get("ArnEquals", {}).get("aws:PrincipalArn")
        customer = (len(principals) == 1 and principals[0].endswith(":root") and actions == ["kms:decrypt"]
                    and isinstance(named, str) and re.fullmatch(r"arn:aws:iam::\d{12}:role/[\w+=,.@/-]+", named) is not None
                    and named.startswith(principals[0][:-len("root")] + "role/"))
        for p in principals:
            if (p == "*" or p.endswith(":root")) and not customer:
                problems.append(f"statement {sid} is account-wide ({p})")
        uses = [a for a in actions if a in ("kms:decrypt", "kms:creategrant", "kms:*", "*") or a.startswith("kms:reencrypt")
                or a in ("kms:d*", "kms:de*")]
        if not uses:
            continue
        if customer:
            customers.append(named)
        elif actions != ["kms:decrypt"] or principals != [sealed_role_arn]:
            problems.append(f"statement {sid} lets {principals} use the key ({actions})")
            continue
        cond = {k.lower(): str(v).lower() for k, v in st.get("Condition", {}).get("StringEqualsIgnoreCase", {}).items()}
        for key, n in _PCR_KEYS.items():
            if cond.get(key) != pcrs[n].lower():
                problems.append(f"PCR{n} is {'not pinned' if key not in cond else 'pinned to another value'}")
        if set(st.get("Condition", {})) - {"StringEqualsIgnoreCase"} - ({"ArnEquals"} if customer else set()):
            problems.append(f"statement {sid} has an unexpected condition operator")
        releases += 1
    if len(customers) > 1:
        problems.append(f"{len(customers)} onboarded customer roles, expected at most 1")
    if releases - len(customers) != 1 and not problems:
        problems.append(f"{releases - len(customers)} release statements for the sealed role, expected exactly 1")
    also = f"; one onboarded customer role ({customers[0].split('/')[-1]}) with the same values" if customers else ""
    return _result(test_id, problems, "one release statement, sealed role only, PCR4/7/12/15 pinned to this build" + also)


def image_registration(image: Dict[str, Any], uefi_data: str) -> Result:
    problems = []
    if image.get("BootMode") != "uefi":
        problems.append(f"boot mode is {image.get('BootMode')}")
    if image.get("TpmSupport") != "v2.0":
        problems.append("TPM not enabled")
    if image.get("Public"):
        problems.append("image is public")
    if len(uefi_data or "") < 100:
        problems.append("no Secure Boot data registered")
    return _result("S4", problems, f"{image.get('ImageId')}: uefi, TPM v2.0, private, Secure Boot data {len(uefi_data)} chars")


FORBIDDEN_PACKAGES = ("openssh-server", "openssh-clients", "amazon-ssm-agent", "cloud-init", "ec2-instance-connect",
                      "telnet", "telnet-server", "rsh", "rsh-server", "dropbear", "cockpit", "cockpit-ws", "xinetd",
                      "vsftpd", "tigervnc-server", "mosh", "tmate", "socat", "nmap-ncat")


def packages(package_list: str, build_log: str) -> Result:
    """S5. `package_list` is KIWI's .packages file: name|epoch|version|... per line."""
    names = {line.split("|")[0].strip() for line in package_list.splitlines() if line.strip()}
    problems = [f"package present: {p}" for p in FORBIDDEN_PACKAGES if p in names]
    if len(names) < 50:
        problems.append("package list looks empty")
    if "CHECK-IMAGE OK" not in build_log:
        problems.append("build log has no CHECK-IMAGE OK line")
    return _result("S5", problems, f"{len(names)} packages, none of the {len(FORBIDDEN_PACKAGES)} forbidden; build check passed")


def role_policies(statements: Iterable[Dict[str, Any]], attached: Sequence[str], allowed: Dict[str, Sequence[str]]) -> Result:
    """S6. `allowed` maps an action to the resource prefixes it may touch."""
    problems, count = [f"managed policy attached: {a}" for a in attached], 0
    for st in statements:
        for action in _list(st.get("Action", [])):
            for resource in _list(st.get("Resource", [])):
                count += 1
                if st.get("Effect") != "Allow":
                    continue
                if "*" in action or action not in allowed:
                    problems.append(f"action not on the list: {action}")
                elif resource == "*" or not any(resource.startswith(p) for p in allowed[action]):
                    problems.append(f"{action} on {resource}")
    if not count:
        problems.append("no statements found")
    return _result("S6", problems, f"{count} action-resource pairs, all on the list; no KMS right; no managed policy")


GATE_PORTS = (443, 8080)     # the inference endpoint: HTTPS on 443; plain 8080 on images before 0.9


def network(igws: list, nats: list, peerings: list, endpoints: Sequence[str], sealed_ingress: list,
            client_sg: Optional[str]) -> Result:
    problems = []
    if igws:
        problems.append("internet gateway attached")
    if [n for n in nats if n.get("State") not in ("deleted", "deleting")]:
        problems.append("NAT gateway present")
    if peerings:
        problems.append("VPC peering present")
    extra = {e.rsplit(".", 1)[-1] for e in endpoints} - {"s3", "kms", "logs"}
    if extra:
        problems.append(f"unexpected endpoints: {sorted(extra)}")
    for rule in sealed_ingress:
        groups = [g.get("GroupId") for g in rule.get("UserIdGroupPairs", [])]
        ok = (rule.get("IpProtocol") == "tcp" and rule.get("FromPort") == rule.get("ToPort") and rule.get("FromPort") in GATE_PORTS
              and not rule.get("IpRanges") and not rule.get("Ipv6Ranges") and client_sg and groups == [client_sg])
        if not ok:
            problems.append(f"inbound rule not allowed: {rule.get('IpProtocol')} {rule.get('FromPort')} from {groups or rule.get('IpRanges')}")
    return _result("S7", problems, f"no gateway, NAT or peering; endpoints {sorted(e.rsplit('.', 1)[-1] for e in endpoints)}; "
                                   f"{len(sealed_ingress)} inbound rule(s), gate from the client group only")


def bucket_objects(heads: Dict[str, str], public_block: Dict[str, bool]) -> Result:
    """S8. `heads` maps an object key to the hex of its first 6 bytes."""
    magic = b"SBOX1\n".hex()
    problems = [f"{key} is not in the encrypted format" for key, head in sorted(heads.items()) if head != magic]
    if not heads:
        problems.append("no objects checked")
    if not (len(public_block) == 4 and all(public_block.values())):
        problems.append("public access is not fully blocked")
    return _result("S8", problems, f"{len(heads)} object(s) carry the encrypted-file header; public access blocked")


def reproducible(first_pcr4: str, second_pcr4: str) -> Result:
    same = bool(first_pcr4) and first_pcr4 == second_pcr4
    return _result("S10", [] if same else [f"PCR4 differs between two builds ({first_pcr4[:12]} vs {second_pcr4[:12]})"],
                   "two builds, same PCR4")


# ---------------------------------------------------------------- console helpers

def _smh(console: str) -> List[str]:
    return [line[line.index("SMH "):].strip() for line in console.splitlines() if "SMH " in line]


def _pcr(lines: Sequence[str], stage: str, n: int) -> Optional[str]:
    for line in lines:
        m = re.match(rf"SMH PCR\[{stage}\]\s+{n}\s*:\s*0x([0-9A-Fa-f]+)", line)
        if m:
            return m.group(1).lower()
    return None


# ---------------------------------------------------------------- live

def good_boot(console: str, summary: Dict[str, str]) -> Result:
    lines, problems = _smh(console), []
    text = "\n".join(lines)
    if text.count("KEY_RELEASED once") != 1:
        problems.append("key was not released exactly once")
    if _pcr(lines, "after", 15) != summary["PCR15_locked"].lower():
        problems.append("runtime value after the lock is not the predicted one")
    if "SMH REPORT answer=" not in text:
        problems.append("no answer from the model")
    if "SMH DONE loader_exit=0" not in text:
        problems.append("loader did not finish cleanly")
    return _result("L1", problems, "key released once; locked value as predicted; model answered")


def launch_inputs(console: str, summary: Dict[str, str], marker: str) -> Result:
    lines, problems = _smh(console), []
    for n in (4, 7, 12):
        if _pcr(lines, "boot", n) != summary[f"PCR{n}"].lower():
            problems.append(f"PCR{n} at boot differs from the build")
    if marker in console:
        problems.append("the user data script ran")
    return _result("L2", problems, "PCR4, 7, 12 equal the build; user data did not run")


def ways_in(console: str, ssm_instances: Sequence[str], instance_id: str, client_console: str) -> Result:
    problems = []
    if re.search(r"login:", console):
        problems.append("a login prompt is on the console")
    if instance_id in ssm_instances:
        problems.append("machine is registered with Systems Manager")
    listening = next((l for l in _smh(console) if l.startswith("SMH REPORT listening=")), "")
    outside = [x for x in re.findall(r"tcp:(\S+)", listening) if not x.startswith(("127.", "[::1]"))]
    if not listening:
        problems.append("no list of listening ports on the console")
    elif len(outside) != 1 or outside[0] not in [f"0.0.0.0:{p}" for p in GATE_PORTS]:
        problems.append(f"listening to the network on: {outside}")
    gate = outside[0].rsplit(":", 1)[1] if len(outside) == 1 else "?"
    for line in _smh(console):          # images from 0.9 on: the model container must have no way out
        if line.startswith("SMH REPORT inside[") and "outbound=" in line and "outbound=blocked" not in line:
            problems.append("a container can open outbound connections: " + line.split("outbound=")[1].split()[0])
    ports = dict(re.findall(r"SMH CLIENT \S+ port (\d+) -> (\w+)", client_console))
    if len(ports) < 10:
        problems.append(f"port scan incomplete ({len(ports)} ports)")
    opened = sorted(p for p, state in ports.items() if state == "open")
    if opened != [gate]:
        problems.append(f"open ports: {opened}")
    return _result("L3", problems, f"no login prompt; not in Systems Manager; {len(ports)} ports scanned, only {gate} open")


def second_request(console: str) -> Result:
    refused = any(line.startswith("SMH REPORT second_key_request=REFUSED") for line in _smh(console))
    return _result("L4", [] if refused else ["second key request was not refused"], "second key request refused")


ALLOWED_PATHS = ("/v1/chat/completions", "/v1/completions", "/v1/models", "/health")


def front_gate(client_console: str, boot_console: str = "") -> Result:
    problems = []
    https = "SMH GATE https" in boot_console               # images from 0.9 on
    if https:
        if not re.search(r"SMH CLIENT \S+ nokey -> 401", client_console):
            problems.append("a request without an API key was not refused with 401")
        mine = re.search(r"cert_sha256=([0-9A-F]{64})", boot_console)
        seen = re.search(r"SMH CLIENT \S+ cert ([0-9A-F]{64})", client_console)
        if not (mine and seen and mine.group(1) == seen.group(1)):
            problems.append("the certificate the client saw is not the one the machine made")
    doors = re.findall(r"door (\w+) (\S+) -> (\d+)", client_console)
    if len(doors) < 10:
        problems.append(f"only {len(doors)} doors tried")
    problems += [f"{method} {path} answered {code}" for method, path, code in doors if code != "404"]
    asks = re.findall(r"ask ([\w ]+?) -> (\d+)", client_console)
    problems += [f"per-token probabilities ({what}) answered {code}" for what, code in asks if code != "400"]
    if len(asks) < 3:
        problems.append("per-token probability requests missing")
    if not re.search(r"models -> 200", client_console):
        problems.append("model list did not answer")
    answers = re.findall(r"answer\[([^\]]+)\] (\d+) (.*)", client_console)
    if not answers or any(code != "200" or not text.strip() for _, code, text in answers):
        problems.append("a question got no answer")
    if not re.search(r"health=200", client_console):
        problems.append("health check did not answer")
    if https and not problems:
        return _result("L5", [], f"HTTPS with the machine's own certificate; no key gets 401; {len(doors)} other doors all 404; {len(asks)} probability requests refused; {len(answers)} answer(s)")
    return _result("L5", problems, f"{len(doors)} other doors all 404; {len(asks)} probability requests refused; {len(answers)} answer(s)")


def job_forms(statuses: Dict[str, Dict[str, Any]], good: str, bad: Sequence[str]) -> Result:
    problems = []
    if statuses.get(good, {}).get("state") != "done":
        problems.append(f"good job {good}: {statuses.get(good, {}).get('state', 'no result')}")
    for job in bad:
        st = statuses.get(job, {})
        if st.get("state") != "refused" or not st.get("problems"):
            problems.append(f"bad job {job}: {st.get('state', 'no result')}")
    return _result("L6", problems, f"good job trained; {len(bad)} bad forms refused, each with a reason")


def job_outputs(listing: Dict[str, int], status: Dict[str, Any], max_bytes: int) -> Result:
    """L7. `listing` maps a file name under adapters/<job>/ to its size."""
    wanted = {"adapter_model.safetensors", "adapter_config.json", "metrics.json"}
    problems = []
    if set(listing) != wanted:
        problems.append(f"files are {sorted(listing)}")
    size = listing.get("adapter_model.safetensors", 0)
    if not 0 < size <= max_bytes:
        problems.append(f"adapter is {size} bytes, limit {max_bytes}")
    if listing.get("adapter_config.json", 0) > 16 << 10 or listing.get("metrics.json", 0) > 64 << 10:
        problems.append("settings or metrics file too large")
    extra = set(status) - {"state", "adapter", "served_as", "adapter_bytes", "final_loss", "steps", "check"}
    if extra:
        problems.append(f"unexpected fields in the result: {sorted(extra)}")
    return _result("L7", problems, f"3 files; adapter {size} bytes (limit {max_bytes})")


def customer_log(events: Sequence[Dict[str, Any]]) -> Result:
    messages = [e.get("message", "") for e in events]
    problems = []
    if not any("SMH LOADER KEY_RELEASED" in m for m in messages):
        problems.append("loader lines missing")
    if not any("SMH LEASE held" in m for m in messages):
        problems.append("lease lines missing")
    if not any(m.startswith("model-server:") for m in messages):
        problems.append("model-server lines missing")
    if not any(m.startswith("kernel: Linux version") for m in messages):
        problems.append("first boot line missing (early boot log not shipped)")
    trainer = [m for m in messages if m.startswith("trainer")]
    if trainer:
        problems.append(f"{len(trainer)} trainer line(s) in the customer's log")
    return _result("L8", problems, f"{len(messages)} lines from the first boot line on; loader, lease, model-server present; no trainer lines")


def flows(events: Sequence[Dict[str, Any]], sealed_ip: str, endpoint_ips: Sequence[str], client_ip: str,
          s3_cidrs: Sequence[str], sealed_eni: Optional[str] = None) -> Result:
    """L9. Version 2 flow log lines. A record whose source port is high and
    destination port is low is the side that opened the connection."""
    nets = [ipaddress.ip_network(c) for c in s3_cidrs]
    problems, out, inn = set(), 0, 0
    rows = [e.get("message", "").split() for e in events]
    rows = [f for f in rows if len(f) >= 14 and f[5].isdigit() and f[6].isdigit()]
    # Every network card in the VPC logs the traffic it sees, so a refused
    # knock shows as ACCEPT on the client's card (it was allowed to leave) and
    # REJECT on the sealed machine's. Only the sealed machine's own card counts.
    # It is the one card that sees the machine talk to addresses outside the VPC (S3).
    if sealed_eni is None:
        cards = {f[2] for f in rows if f[3] == sealed_ip and not f[4].startswith("10.")}
        sealed_eni = cards.pop() if len(cards) == 1 else None
    if sealed_eni is None:
        return _result("L9", ["could not tell which network card is the sealed machine's"], "")
    for f in rows:
        if f[2] != sealed_eni:
            continue
        src, dst, sport, dport, action = f[3], f[4], int(f[5]), int(f[6]), f[12]
        if action != "ACCEPT" or not (sport > 1024 and dport <= 8080):
            continue
        if src == sealed_ip:
            out += 1
            if not (dst in endpoint_ips and dport == 443) and not (dport == 443 and any(ipaddress.ip_address(dst) in n for n in nets)):
                problems.add(f"outbound to {dst}:{dport}")
        elif dst == sealed_ip:
            inn += 1
            if not (src == client_ip and dport in GATE_PORTS):
                problems.add(f"inbound from {src} to port {dport}")
    if not out:
        problems.add("no outbound records found")
    return _result("L9", sorted(problems), f"{out} outbound records: KMS, Logs and S3 only; {inn} inbound: client to the gate only")


def usage(check_output: str) -> Result:
    """L10. The text printed by image/pipeline/check-usage.sh plus the two tamper runs."""
    problems = []
    first = re.search(r"(\d+) record\(s\), (\d+) problem\(s\), seals checked", check_output)
    if not first or int(first.group(1)) == 0 or first.group(2) != "0":
        problems.append("the builder's seal check did not pass")
    if len(re.findall(r"builder cannot list \S+ \(expected\)", check_output)) != 4 or "PROBLEM: builder can list" in check_output:
        problems.append("the builder can list another folder")
    changed = check_output.split("change one number", 1)[-1].split("delete one record", 1)[0]
    deleted = check_output.split("delete one record", 1)[-1].split("customer view", 1)[0]
    if "seal does not match" not in changed:
        problems.append("a changed record was not caught")
    if "are missing" not in deleted:
        problems.append("a deleted record was not caught")
    return _result("L10", problems, f"{first.group(1) if first else 0} records, seals pass; changed and deleted copies caught; "
                                    "other folders not listable")


def _trail(events: Dict[str, Any], instance_id: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = [json.loads(e["CloudTrailEvent"]) for e in events.get("Events", [])]
    if instance_id:
        rows = [r for r in rows if instance_id in json.dumps(r.get("userIdentity", {}))]
    return rows


def cloudtrail(model_events: Dict[str, Any], lease_events: Dict[str, Any], instance_id: str, boots: int) -> Result:
    model = [r for r in _trail(model_events, instance_id) if r["eventName"] == "Decrypt" and "errorCode" not in r]
    lease = [r for r in _trail(lease_events, instance_id) if r["eventName"] == "Decrypt" and "errorCode" not in r]
    problems = []
    if len(model) != boots:
        problems.append(f"model key released {len(model)} time(s) in {boots} boot(s)")
    if len(lease) < 2:
        problems.append(f"only {len(lease)} lease renewal(s)")
    return _result("L11", problems, f"model key released {len(model)} time(s) in {boots} boot(s); {len(lease)} lease renewals")


def disk_unchanged(image_sha: str, disk_sha: str) -> Result:
    same = len(image_sha) == 64 and image_sha == disk_sha
    return _result("L12", [] if same else [f"disk differs from the image ({disk_sha[:12]} vs {image_sha[:12]})"],
                   f"disk equals the released image (sha256 {image_sha[:12]}...)")


def _seconds(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def cut_off(disabled_at: str, client_console: str, final_state: str, bound_seconds: int) -> Result:
    """L13. Times are HH:MM:SS UTC on one day."""
    ticks = [(t, code) for t, code in re.findall(r"SMH CLIENT (\d\d:\d\d:\d\d) tick (\d+)", client_console)
             if _seconds(t) >= _seconds(disabled_at)]
    problems = []
    failed = [t for t, code in ticks if code != "200"]
    if not failed:
        problems.append("the machine kept answering")
        took = -1
    else:
        took = _seconds(failed[0]) - _seconds(disabled_at)
        if took > bound_seconds:
            problems.append(f"stopped answering after {took} s, bound {bound_seconds} s")
        if any(code == "200" for t, code in ticks if _seconds(t) > _seconds(failed[0])):
            problems.append("answered again after the cut-off")
    if final_state not in ("stopped", "stopping"):
        problems.append(f"machine is {final_state}, not powered off")
    return _result("L13", problems, f"stopped answering within {took} s (bound {bound_seconds} s); machine powered off")


# ---------------------------------------------------------------- break

def no_key(test_id: str, console: str, model_events: Dict[str, Any], instance_id: str, final_state: str,
           must_see: Sequence[str] = (), no_request: bool = False, must_power_off: bool = False) -> Result:
    """B1 to B5: whatever else happens, this machine must not get the key or answer."""
    text = "\n".join(_smh(console))
    rows = _trail(model_events, instance_id)
    problems = []
    if "KEY_RELEASED" in text or [r for r in rows if r["eventName"] == "Decrypt" and "errorCode" not in r]:
        problems.append("THE KEY WAS RELEASED")
    if "SMH REPORT answer=" in text:
        problems.append("the model answered")
    if must_see and not any(s in console for s in must_see):
        problems.append(f"expected sign not seen ({' or '.join(must_see)})")
    if no_request and [r for r in rows if r["eventName"] == "Decrypt"]:
        problems.append("a key request was made")
    if must_power_off and final_state == "running":   # lease refused too: the machine must switch itself off
        problems.append("machine is still running")
    seen = next((s for s in must_see if s in console), "no key")
    return _result(test_id, problems, f"no key released, no answer; saw: {seen}; machine {final_state}")


def remote_management(console: str, model_events: Dict[str, Any], instance_id: str, final_state: str,
                      ssm_instances: Sequence[str], endpoints: Sequence[str]) -> Result:
    """B7. The key-refused line also proves the attack was live: the role and the network both worked."""
    r = no_key("B7", console, model_events, instance_id, final_state, ["key release failed"], must_power_off=True)
    problems = [] if r.verdict == "PASS" else [r.seen]
    if len([e for e in endpoints if e.rsplit(".", 1)[-1] in ("ssm", "ssmmessages", "ec2messages")]) != 3:
        problems.append(f"the three Systems Manager endpoints were not in place ({len(endpoints)} found)")
    if instance_id in ssm_instances:
        problems.append("MACHINE REGISTERED WITH SYSTEMS MANAGER")
    return _result("B7", problems, f"role with Systems Manager rights and 3 endpoints in place; machine never registered; no key; machine {final_state}")


# Not the bare "PM: hibernation": every boot prints "PM: hibernation: Registered nosave memory".
HIBERNATE_SIGNS = ("hibernation entry", "Creating hibernation image", "Creating image", "PM: Image saving", "Hibernating")
TAIL_SLACK = 1 << 20     # a partition-table copy at the end of the disk is a few KB; a memory image is GBs


BOOT_PART_MIB = range(1, 201)   # the 200 MiB boot partition (image/appliance.kiwi: efipartsize)
UNCLEAN_STOP_BYTES = 16         # a forced stop leaves the boot partition's "not cleanly unmounted" mark: 1 byte seen


def hibernation(console: str, aws_answer: str, image_sha: str, disk_sha: str, tail_nonzero: Any,
                diff_bytes: Any = None, diff_mib: Any = None) -> Result:
    """B8. Whatever AWS and the machine did with the request, the disk must hold the image and no memory image.

    The machine is stopped by force here, so the boot partition (not secret, not trusted: Secure Boot checks
    what is started from it) may carry a few changed bytes. Anything more, or anywhere else, fails."""
    problems = []
    if not _smh(console):
        problems.append("the machine did not boot, so nothing was tested")
    same = len(image_sha) == 64 and image_sha == disk_sha
    mark = (not same and isinstance(diff_bytes, int) and 0 < diff_bytes <= UNCLEAN_STOP_BYTES
            and bool(diff_mib) and all(m in BOOT_PART_MIB for m in diff_mib))
    if not same and not mark:
        problems.append(f"disk differs from the image ({disk_sha[:12]} vs {image_sha[:12]}; {diff_bytes} byte(s) in megabyte(s) {diff_mib})")
    if not isinstance(tail_nonzero, int):
        problems.append("the space after the image was not read")
    elif tail_nonzero >= TAIL_SLACK:
        problems.append(f"{tail_nonzero} bytes of data after the image")
    sign = next((s for s in HIBERNATE_SIGNS if s in console), "")
    if sign:
        problems.append(f"the machine started to hibernate ({sign})")
    key = "key was in memory" if "KEY_RELEASED" in console else "no key in memory at the time"
    disk = "disk equals the image" if same else f"disk equals the image except {diff_bytes} byte(s) in the boot partition (megabyte {diff_mib}), no memory image"
    return _result("B8", problems, f"{key}; {disk}, {tail_nonzero} bytes after it; AWS: {' '.join(aws_answer.split())[-160:]}")


def tampered_weights(console: str, client_console: str, final_state: str, must_power_off: bool = False) -> Result:
    text = "\n".join(_smh(console))
    problems = []
    if "SMH REPORT answer=" in text or "MODEL READY" in text:
        problems.append("the model came up on tampered weights")
    if re.search(r"SMH CLIENT \S+ (tick|health=)\s*200", client_console) or "health=200" in client_console:
        problems.append("the gate answered")
    if must_power_off and final_state == "running":
        problems.append("machine is still running with no model")
    return _result("B6", problems, f"model never ready, nothing answered; machine {final_state}")


def builder_on_customer(statements: Iterable[Dict[str, Any]], customer_bucket: str) -> Result:
    """S9. Every right the builder's role has on the customer's bucket must be about usage/."""
    arn, problems, found = f"arn:aws:s3:::{customer_bucket}", [], 0
    for st in statements:
        if st.get("Effect") != "Allow":
            continue
        for resource in _list(st.get("Resource", [])):
            if resource == "*" and any(str(a).startswith(("s3:", "*")) for a in _list(st.get("Action", []))):
                problems.append("an S3 right on every bucket")
            if not resource.startswith(arn):
                continue
            found += 1
            for action in _list(st.get("Action", [])):
                prefixes = _list(st.get("Condition", {}).get("StringLike", {}).get("s3:prefix", []))
                if action == "s3:GetObject" and resource == arn + "/usage/*":
                    continue
                if action == "s3:ListBucket" and resource == arn and prefixes == ["usage/*"]:
                    continue
                problems.append(f"{action} on {resource}")
    good = (f"{found} statement(s) on the customer's bucket, all limited to usage/" if found
            else "no rights on the customer's bucket at all (the control stack is not deployed)")
    return _result("S9", problems, good)


def unit_tests(output: str) -> Result:
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", output))
    bad = re.findall(r"(\d+) (failed|error)", output)
    return _result("S1", ([f"{n} {what}" for n, what in bad] or []) + ([] if passed else ["no tests ran"]), f"{passed} tests passed")


def restored(console: str, summary: Dict[str, str]) -> Result:
    """R1. The loader checks the container archive's fingerprint and every chunk of the weights, so a good boot proves both objects."""
    r = good_boot(console, summary)
    return Result("R1", r.verdict, r.seen if r.verdict != "PASS" else "good boot after the swaps: archive fingerprint and weights accepted, model answered")


def deployer_roles(roles: Dict[str, Dict[str, Any]], bucket: str) -> Result:
    """S11. `roles` maps the three deployer role names to {"statements": [...], "attached": [...]}.
    If the deployer is not installed there is nothing to check, and that is said."""
    if not roles:
        return Result("S11", "PASS", "the deployer is not installed: no agent roles exist")
    problems = []
    for name, role in sorted(roles.items()):
        if role.get("attached"):
            problems.append(f"{name}: managed policy attached")
        allows = [st for st in role["statements"] if st.get("Effect") == "Allow"]
        denies = [st for st in role["statements"] if st.get("Effect") == "Deny"]
        for st in allows:
            for action in _list(st.get("Action", [])):
                low = str(action).lower()
                if low.startswith("kms:") or low in ("*", "iam:*", "s3:*", "ec2:*", "ssm:*"):
                    problems.append(f"{name}: {action}")
                if low == "ssm:sendcommand":
                    for resource in _list(st.get("Resource", [])):
                        if ":document/" in resource and not resource.endswith(":document/smh-poc-pipeline-step"):
                            problems.append(f"{name}: may run another document ({resource})")
                        if resource == "*":
                            problems.append(f"{name}: may run any document anywhere")
                        if ":instance/" in resource and "ssm:resourceTag/app" not in json.dumps(st.get("Condition", {})):
                            problems.append(f"{name}: may send commands to untagged machines")
                if low in ("s3:putobject", "s3:deleteobject"):
                    for resource in _list(st.get("Resource", [])):
                        if not resource.startswith(f"arn:aws:s3:::{bucket}/deploy/"):
                            problems.append(f"{name}: may write {resource}")
        if "tools" in name:
            denied = {str(r) for st in denies for r in _list(st.get("Resource", [])) if "s3:PutObject" in _list(st.get("Action", []))}
            if f"arn:aws:s3:::{bucket}/approvals/*" not in denied:
                problems.append(f"{name}: no explicit deny on writing approvals")
    return _result("S11", problems, f"{len(roles)} deployer roles: no KMS right, writes limited to deploy/, approvals denied, one fixed document on tagged machines")

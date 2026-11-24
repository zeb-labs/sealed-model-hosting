"""Judge one run folder and write its report.

    python3 assure/report.py evidence/m6/<run>

Reads whatever evidence `assure.sh` saved in the folder, runs the matching
check, and writes report.md and results.json there. A test whose evidence is
missing or unreadable is NOT RUN, with the reason. Exit code 0 only if
nothing failed and nothing was left out (known open items aside).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import checks  # noqa: E402

KNOWN_OPEN = {"S10": "Reproducible build, open since M2"}
ACCOUNT = "111122223333"
SEALED_ROLE = f"arn:aws:iam::{ACCOUNT}:role/zl/smh-poc-sealed-instance-role"
CUSTOMER_BUCKET = f"smh-poc-customer-{ACCOUNT}"
ROLE_ALLOWED = {
    "s3:GetObject": [f"arn:aws:s3:::smh-poc-artifacts-{ACCOUNT}/", f"arn:aws:s3:::{CUSTOMER_BUCKET}/"],
    "s3:PutObject": [f"arn:aws:s3:::{CUSTOMER_BUCKET}/"],
    "s3:ListBucket": [f"arn:aws:s3:::{CUSTOMER_BUCKET}"],
    "logs:CreateLogStream": [f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/smh-poc/customer/"],
    "logs:PutLogEvents": [f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/smh-poc/customer/"],
}
USERDATA_MARKER = "SMH-USERDATA-RAN"
BREAK_SIGNS = {
    "B1": (["key release failed"], False, True),
    "B2": (["key release failed", "Security Violation", "Access Denied"], False, True),
    "B3": (["dm-verity device corrupted", "is corrupted", "Kernel panic"], False, False),
    "B4": (["key release failed"], False, True),
    "B5": (["does not match the runtime list"], True, True),
}


def judge(run: str) -> List[checks.Result]:
    def path(*parts: str) -> str:
        return os.path.join(run, *parts)

    def text(*parts: str) -> str:
        with open(path(*parts), errors="replace") as f:
            return f.read()

    def data(*parts: str) -> Any:
        return json.loads(text(*parts))

    results: List[checks.Result] = []

    def run_check(test_id: str, evidence: str, fn: Callable[[], checks.Result]) -> None:
        try:
            r = fn()
            r.evidence = evidence
        except FileNotFoundError as error:
            r = checks.Result(test_id, "NOT RUN", f"no evidence: {os.path.relpath(error.filename, run)}", evidence)
        except Exception as error:  # unreadable evidence is not a pass
            r = checks.Result(test_id, "NOT RUN", f"evidence unreadable: {type(error).__name__}: {str(error)[:120]}", evidence)
        results.append(r)

    def summary() -> Dict[str, str]:
        return data("static", "summary.json")

    def pcrs(state: str) -> Dict[int, str]:
        s = summary()
        return {4: s["PCR4"], 7: s["PCR7"], 12: s["PCR12"], 15: s[f"PCR15_{state}"]}

    # ---- static
    run_check("S1", "static/unit-tests.txt", lambda: checks.unit_tests(text("static", "unit-tests.txt")))
    run_check("S2", "static/model-key-policy.json",
              lambda: checks.key_policy("S2", data("static", "model-key-policy.json"), SEALED_ROLE, pcrs("open")))
    run_check("S3", "static/lease-key-policy.json",
              lambda: checks.key_policy("S3", data("static", "lease-key-policy.json"), SEALED_ROLE, pcrs("locked")))
    run_check("S4", "static/image.json", lambda: checks.image_registration(data("static", "image.json"), text("static", "uefi-data.txt").strip()))
    run_check("S5", "static/packages.txt", lambda: checks.packages(text("static", "packages.txt"), text("static", "pipeline.log")))
    run_check("S6", "static/sealed-role.json", lambda: checks.role_policies(
        data("static", "sealed-role.json")["statements"], data("static", "sealed-role.json")["attached"], ROLE_ALLOWED))
    run_check("S7", "static/network.json", lambda: checks.network(**data("static", "network.json")))
    run_check("S8", "static/bucket.json", lambda: checks.bucket_objects(**data("static", "bucket.json")))
    run_check("S9", "static/builder-role.json",
              lambda: checks.builder_on_customer(data("static", "builder-role.json")["statements"], CUSTOMER_BUCKET))
    run_check("S11", "static/deployer-roles.json",
              lambda: checks.deployer_roles(data("static", "deployer-roles.json"), f"smh-poc-artifacts-{ACCOUNT}"))
    run_check("S10", "static/repro.json", lambda: checks.reproducible(**data("static", "repro.json")))

    # ---- live
    def ids() -> Dict[str, Any]:
        return data("live", "ids.json")

    def max_adapter() -> int:
        per_rank = summary()["artifacts"]["params_per_rank"]
        form = data("live", "good-form.json")
        return sum(per_rank[m] for m in form["target_modules"]) * form["rank"] * 4 + (1 << 16)

    def statuses() -> Dict[str, Any]:
        out = {}
        for job in os.listdir(path("live", "jobs")):
            out[job] = data("live", "jobs", job, "status.json")
        return out

    boot = "live/boot-console.txt"
    run_check("L1", boot, lambda: checks.good_boot(text("live", "boot-console.txt"), summary()))
    run_check("L2", boot, lambda: checks.launch_inputs(text("live", "boot-console.txt"), summary(), USERDATA_MARKER))
    run_check("L3", "live/client-console.txt", lambda: checks.ways_in(
        text("live", "boot-console.txt"), data("live", "ssm.json"), ids()["instance"], text("live", "client-console.txt")))
    run_check("L4", boot, lambda: checks.second_request(text("live", "boot-console.txt")))
    run_check("L5", "live/client-console.txt", lambda: checks.front_gate(text("live", "client-console.txt"), text("live", "boot-console.txt")))
    run_check("L6", "live/jobs/", lambda: checks.job_forms(statuses(), ids()["good_job"], ids()["bad_jobs"]))
    run_check("L7", "live/adapter-listing.json", lambda: checks.job_outputs(
        data("live", "adapter-listing.json"), statuses()[ids()["good_job"]], max_adapter()))
    run_check("L8", "live/customer-log.json", lambda: checks.customer_log(data("live", "customer-log.json")["events"]))
    run_check("L9", "live/flow-logs.json", lambda: checks.flows(
        data("live", "flow-logs.json")["events"], ids()["sealed_ip"], ids()["endpoint_ips"], ids()["client_ip"], ids()["s3_cidrs"], ids().get("sealed_eni")))
    run_check("L10", "live/usage-check.txt", lambda: checks.usage(text("live", "usage-check.txt")))
    run_check("L11", "live/cloudtrail-model.json", lambda: checks.cloudtrail(
        data("live", "cloudtrail-model.json"), data("live", "cloudtrail-lease.json"), ids()["instance"], ids()["boots"]))
    run_check("L12", "live/disk.json", lambda: checks.disk_unchanged(data("live", "disk.json")["image_sha"], data("live", "disk.json")["disk_sha"]))
    run_check("L13", "live/cutoff.json", lambda: checks.cut_off(
        data("live", "cutoff.json")["disabled_at"], text("live", "client-console-after-cutoff.txt"),
        data("live", "cutoff.json")["final_state"], data("live", "cutoff.json")["bound_seconds"]))

    # ---- break
    for test_id, (signs, no_request, power_off) in BREAK_SIGNS.items():
        d = ("break", test_id)
        run_check(test_id, f"break/{test_id}/console.txt", lambda d=d, t=test_id, s=signs, n=no_request, p=power_off: checks.no_key(
            t, text(*d, "console.txt"), data(*d, "cloudtrail-model.json"), text(*d, "instance.txt").strip(),
            text(*d, "state.txt").strip(), s, no_request=n, must_power_off=p))
    run_check("B6", "break/B6/console.txt", lambda: checks.tampered_weights(
        text("break", "B6", "console.txt"), text("break", "B6", "client-console.txt"), text("break", "B6", "state.txt").strip(),
        must_power_off="no model: powering off" in text("break", "B6", "console.txt")))   # images from 0.8.0 on
    run_check("B7", "break/B7/ssm.json", lambda: checks.remote_management(
        text("break", "B7", "console.txt"), data("break", "B7", "cloudtrail-model.json"), text("break", "B7", "instance.txt").strip(),
        text("break", "B7", "state.txt").strip(), data("break", "B7", "ssm.json"), data("break", "B7", "endpoints.json")))
    run_check("B8", "break/B8/disk.json", lambda: checks.hibernation(
        text("break", "B8", "console.txt") + text("break", "B8", "console-before.txt"), text("break", "B8", "hibernate.txt"), **data("break", "B8", "disk.json")))
    run_check("R1", "break/R1/console.txt", lambda: checks.restored(text("break", "R1", "console.txt"), summary()))
    return results


def write(run: str, results: List[checks.Result]) -> int:
    count = {v: sum(r.verdict == v for r in results) for v in ("PASS", "FAIL", "NOT RUN")}
    open_items = [r for r in results if r.verdict != "PASS" and r.id in KNOWN_OPEN]
    bad = [r for r in results if r.verdict != "PASS" and r.id not in KNOWN_OPEN]
    meta = {}
    try:
        meta = json.load(open(os.path.join(run, "run.json")))
    except Exception:
        pass
    lines = [f"# Assurance report: {os.path.basename(os.path.normpath(run))}", "",
             f"- Image: `{meta.get('ami', 'unknown')}`, version {meta.get('version', 'unknown')}",
             f"- Started (UTC): {meta.get('started', 'unknown')}; tiers asked for: {meta.get('tiers', 'unknown')}",
             f"- Result: **{count['PASS']} pass, {count['FAIL']} fail, {count['NOT RUN']} not run** of {len(results)} tests",
             f"- Verdict: **{'CLEAN' if not bad else 'NOT CLEAN'}**"
             + (f" (with {len(open_items)} known open item(s))" if open_items else ""), "",
             "Every PASS below was judged from the raw file named in the last column. NOT RUN is never a pass.", "",
             "| # | Property | Must be true | Result | What was seen | Evidence |", "| --- | --- | --- | --- | --- | --- |"]
    for r in results:
        verdict = r.verdict + (" (known open)" if r in open_items else "")
        lines.append(f"| {r.id} | {r.prop} | {r.expected} | {verdict} | {r.seen.replace('|', '/')} | `{r.evidence}` |")
    if open_items:
        lines += ["", "## Known open items", ""] + [f"- {r.id}: {KNOWN_OPEN[r.id]}. {r.seen}" for r in open_items]
    with open(os.path.join(run, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(run, "results.json"), "w") as f:
        json.dump([{"id": r.id, "property": r.prop, "expected": r.expected, "verdict": r.verdict, "seen": r.seen,
                    "evidence": r.evidence} for r in results], f, indent=1)
    print(f"{count['PASS']} pass, {count['FAIL']} fail, {count['NOT RUN']} not run -> {os.path.join(run, 'report.md')}")
    for r in bad:
        print(f"  {r.verdict} {r.id}: {r.seen[:150]}")
    return 1 if bad else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2].strip())
        raise SystemExit(2)
    raise SystemExit(write(sys.argv[1], judge(sys.argv[1])))

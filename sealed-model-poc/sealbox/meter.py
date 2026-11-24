"""The meter: a fixed usage record, numbers only, that both sides can check.

Every window the sealed machine reads the model server's own counters and
writes one small record into the CUSTOMER's bucket. The customer can read
every byte that the builder will see. Each record carries a seal (HMAC) made
with a secret that only the sealed machine and the builder hold, so the
builder can tell if a record was changed, and the numbering shows a gap if
one was removed.

A record is written every window even when nothing was asked, so a silent
stretch is visible too.

    python3 -m sealbox.meter check <folder>      # seal key as hex on standard input, or empty for shape only
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Callable, Dict, Iterable, List, Optional

COUNTS = ("requests", "tokens_in", "tokens_out")
TEXT = ("instance", "boot", "image", "start", "end")
FIELDS = {"v", "seq", "seal", *COUNTS, *TEXT}
# Version 2 adds the machine's OWN requests in the window (boot report, job
# self-checks), so a builder can bill requests minus own_requests.
FIELDS_V2 = FIELDS | {"own_requests"}
OWN_FILE = "/run/smh-own-requests"


def note_own(n: int = 1, path: str = OWN_FILE) -> None:
    """Called by the machine's own checks after they send a request to the model server."""
    import fcntl

    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        total = int(f.read().strip() or 0) + n
        f.seek(0); f.truncate(); f.write(str(total))


def own_total(path: str = OWN_FILE) -> int:
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0
MAX_TEXT = 64


def parse_counters(text: str, names: Dict[str, str]) -> Dict[str, int]:
    """Sum each named counter over all its label sets in a Prometheus text page."""
    totals = {field: 0 for field in names}
    seen = set()
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        head, _, value = line.rpartition(" ")
        metric = head.split("{", 1)[0].strip()
        for field, name in names.items():
            if metric in (name, name + "_total"):
                totals[field] += int(float(value))
                seen.add(field)
    missing = set(names) - seen
    if missing:
        raise ValueError("counter not found: " + ", ".join(sorted(missing)))
    return totals


def _body(record: Dict[str, Any]) -> bytes:
    return json.dumps({k: v for k, v in record.items() if k != "seal"}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True).encode("ascii")


def seal(record: Dict[str, Any], key: bytes) -> Dict[str, Any]:
    return {**record, "seal": hmac.new(key, _body(record), hashlib.sha256).hexdigest()}


def make_record(seq: int, instance: str, boot: str, image: str, start: str, end: str,
                before: Dict[str, int], now: Dict[str, int], key: bytes, own: Optional[int] = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {"v": 1 if own is None else 2, "seq": seq, "instance": instance, "boot": boot, "image": image,
                              "start": start, "end": end}
    if own is not None:
        record["own_requests"] = own
    for field in COUNTS:
        # A counter that went down means the model server restarted: count from zero.
        record[field] = now[field] - before[field] if now[field] >= before[field] else now[field]
    return seal(record, key)


def _shape(record: Any) -> Optional[str]:
    if not isinstance(record, dict) or record.get("v") not in (1, 2):
        return "unknown version"
    if set(record) != (FIELDS if record["v"] == 1 else FIELDS_V2):
        return "fields are not exactly the fixed set"
    for field in ("seq", *COUNTS, *(("own_requests",) if record["v"] == 2 else ())):
        if type(record[field]) is not int or record[field] < 0:
            return f"{field} is not a count"
    for field in TEXT:
        if not isinstance(record[field], str) or len(record[field]) > MAX_TEXT:
            return f"{field} is not a short text"
    return None


def check(records: Iterable[Any], key: Optional[bytes] = None) -> List[str]:
    """Problems found in a set of records. Empty list: all good.

    Without the key (the customer's view): shape and numbering only.
    """
    problems: List[str] = []
    boots: Dict[tuple, List[int]] = {}
    for record in records:
        bad = _shape(record)
        if bad:
            problems.append(f"record refused: {bad}")
            continue
        where = f"{record['instance']} {record['boot']} #{record['seq']}"
        if key is not None and not hmac.compare_digest(seal(record, key)["seal"].encode(), str(record["seal"]).encode("utf-8", "replace")):
            problems.append(f"{where}: seal does not match (changed or forged)")
            continue
        boots.setdefault((record["instance"], record["boot"]), []).append(record["seq"])
    for (instance, boot), seqs in sorted(boots.items()):
        seqs.sort()
        for a, b in zip([-1] + seqs, seqs):
            if b == a:
                problems.append(f"{instance} {boot}: record #{b} appears twice")
            elif b != a + 1:
                problems.append(f"{instance} {boot}: records #{a + 1} to #{b - 1} are missing")
    return problems


def totals(records: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out = {field: 0 for field in (*COUNTS, "own_requests")}
    for record in records:
        for field in COUNTS:
            out[field] += record[field]
        out["own_requests"] += record.get("own_requests", 0)
    return out


# ---------------------------------------------------------------------------
# The real machine. Thin wrappers; exercised on AWS.
# ---------------------------------------------------------------------------

def real_hook(cfg: Dict[str, Any], say: Callable[[str], None]) -> Callable[[bytes], None]:
    """Called by the lease service after each renewal, with the seal key."""
    import datetime as dt
    import time
    import urllib.request

    import boto3

    u = cfg["usage"]
    s3 = boto3.client("s3", region_name=cfg["region"])

    def imds(path: str) -> str:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        tok = urllib.request.urlopen(req, timeout=3).read().decode()
        req = urllib.request.Request("http://169.254.169.254/latest/meta-data/" + path,
                                     headers={"X-aws-ec2-metadata-token": tok})
        return urllib.request.urlopen(req, timeout=3).read().decode()

    def stamp() -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    state = {"seq": 0, "before": {f: 0 for f in COUNTS}, "own": 0, "start": stamp(), "due": time.monotonic() + u["every"],
             "instance": imds("instance-id"), "boot": stamp()}

    def on_renewed(key: bytes) -> None:
        state["key"] = key              # kept in RAM for the final record
        if time.monotonic() < state["due"]:
            return
        write(key)

    def final() -> None:
        """The last, shorter window before the machine stops. Without it that use is never counted."""
        if state.get("key") and not state.get("closed"):
            state["closed"] = True
            write(state["key"])

    def write(key: bytes) -> None:
        state["due"] = time.monotonic() + u["every"]
        now = parse_counters(urllib.request.urlopen(u["metrics_url"], timeout=10).read().decode(), u["counters"])
        end = stamp()
        own = own_total()
        record = make_record(state["seq"], state["instance"], state["boot"], u["image"], state["start"], end,
                             state["before"], now, key, own=max(own - state["own"], 0))
        state["own"] = own
        s3.put_object(Bucket=u["bucket"], Body=json.dumps(record, indent=1).encode(),
                      Key=f"usage/{state['instance']}/{state['boot']}/{state['seq']:08d}.json",
                      ServerSideEncryption="aws:kms", SSEKMSKeyId=u["kms_key"])
        say(f"USAGE record #{state['seq']} written: {record['requests']} requests")
        state.update(seq=state["seq"] + 1, before=now, start=end)

    on_renewed.final = final  # type: ignore[attr-defined]
    return on_renewed


def main(argv: List[str]) -> int:
    import glob
    import sys

    if argv[:1] == ["own"]:            # python3 -m sealbox.meter own [n]: used by the boot report
        note_own(int(argv[1]) if len(argv) > 1 else 1)
        return 0
    if len(argv) != 2 or argv[0] != "check":
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    hexkey = sys.stdin.read().strip()
    records = [json.load(open(path)) for path in sorted(glob.glob(argv[1] + "/**/*.json", recursive=True))]
    problems = check(records, bytes.fromhex(hexkey) if hexkey else None)
    for line in problems:
        print("PROBLEM " + line)
    good = [r for r in records if _shape(r) is None]
    print(f"{len(records)} record(s), {len(problems)} problem(s), "
          f"{'seals checked' if hexkey else 'shape and numbering only'}; totals {totals(good)}")
    return 1 if problems else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))

"""The log shipper: the machine's own log goes to the CUSTOMER's log group.

Sent: kernel, boot, loader, job runner, lease and model-server lines.
Never sent: anything the trainer container prints. Values printed during
training steps are the risky ones (docs/m4-leak-bound.md), so the trainer's
only way out stays the checked metrics file.

Two caps keep the log from becoming a wide channel: a longest line, and a
number of bytes per hour. Lines over the cap are dropped and counted.

The builder gets none of this: the machine's role may write to the customer's
log group only.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

NEVER = ("trainer",)          # container names whose output never leaves
MAX_LINE = 1000
MAX_HELD = 5000               # lines kept while the log service cannot be reached

Event = Tuple[int, str]       # (milliseconds since 1970, text)


def pick(entry: Dict[str, Any]) -> Optional[Event]:
    """One journal entry -> one log line, or None if it must not be sent."""
    container = entry.get("CONTAINER_NAME")
    if container in NEVER:
        return None
    text = entry.get("MESSAGE")
    if not isinstance(text, str) or not text.strip():   # binary entries arrive as number lists: skip
        return None
    source = container or entry.get("SYSLOG_IDENTIFIER") or "system"
    try:
        ms = int(entry["__REALTIME_TIMESTAMP"]) // 1000
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    return ms, f"{str(source)[:40]}: {text[:MAX_LINE]}"


class Shipper:
    def __init__(self, send: Callable[[List[Event]], None], now: Callable[[], float], max_bytes_per_hour: int):
        self._send, self._now, self._max = send, now, max_bytes_per_hour
        self._held: List[Event] = []
        self._window, self._used, self.dropped = now(), 0, 0

    def add(self, entry: Dict[str, Any]) -> None:
        event = pick(entry)
        if event is None:
            return
        if self._now() - self._window >= 3600:
            if self.dropped:
                self._held.append((event[0], f"smh-logship: {self.dropped} line(s) dropped by the hourly cap"))
            self._window, self._used, self.dropped = self._now(), 0, 0
        size = len(event[1].encode())
        if self._used + size > self._max or len(self._held) >= MAX_HELD:
            self.dropped += 1
            return
        self._used += size
        self._held.append(event)

    def flush(self) -> None:
        if not self._held:
            return
        batch = sorted(self._held[:500], key=lambda e: e[0])   # the log service wants time order
        self._send(batch)                    # on failure the lines stay held for the next try
        del self._held[:500]


# ---------------------------------------------------------------------------
# The real machine. Thin wrappers; exercised on AWS.
# ---------------------------------------------------------------------------

def main() -> int:
    import datetime as dt
    import json
    import os
    import select
    import subprocess
    import time
    import urllib.request

    import boto3

    from . import loader

    with open("/etc/smh/loader.json") as f:
        cfg = loader.site(json.load(f))
    logs = boto3.client("logs", region_name=cfg["region"])
    group = cfg["logs"]["group"]
    req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                 headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    tok = urllib.request.urlopen(req, timeout=3).read().decode()
    req = urllib.request.Request("http://169.254.169.254/latest/meta-data/instance-id",
                                 headers={"X-aws-ec2-metadata-token": tok})
    stream = urllib.request.urlopen(req, timeout=3).read().decode() + "/" + \
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    made = [False]

    def send(batch: List[Event]) -> None:
        if not made[0]:
            logs.create_log_stream(logGroupName=group, logStreamName=stream)
            made[0] = True
        logs.put_log_events(logGroupName=group, logStreamName=stream,
                            logEvents=[{"timestamp": ms, "message": text} for ms, text in batch])

    shipper = Shipper(send, time.monotonic, cfg["logs"]["max_bytes_per_hour"])
    # "-n all": without it, follow mode starts with only the last 10 lines of the boot.
    journal = subprocess.Popen(["journalctl", "-b", "-f", "-n", "all", "-o", "json", "--no-pager"], stdout=subprocess.PIPE)
    fd, pending, last = journal.stdout.fileno(), b"", time.monotonic()
    while True:
        if select.select([fd], [], [], 5)[0]:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return 1                      # the journal reader ended; systemd restarts this service
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                try:
                    shipper.add(json.loads(line))
                except ValueError:
                    pass
        if time.monotonic() - last >= 5:
            last = time.monotonic()
            try:
                shipper.flush()
            except Exception as error:
                print(f"SMH LOGS not sent: {type(error).__name__}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())

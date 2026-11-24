"""API keys for the front gate. The keys are the CUSTOMER's.

The customer keeps a small text file in their own bucket, one key per line.
Every 30 seconds the sealed machine reads it and tells the gate which keys to
accept. No file, or no valid key in it, means every request is refused (401).

The file comes from the party we defend against, so nothing in it is trusted:
a key must be 24 to 128 characters of letters, digits, "-" and "_". Anything
else is ignored. At most 50 keys. The keys never appear in a log.
"""

from __future__ import annotations

import re
from typing import Tuple

KEY = re.compile(r"^[A-Za-z0-9_-]{24,128}$")
MAX_KEYS, MAX_BYTES = 50, 16 << 10


def render(text: str) -> Tuple[str, int, int]:
    """The customer's key file -> (the gate's map file, keys accepted, lines ignored)."""
    accepted, ignored = [], 0
    for line in text[:MAX_BYTES].splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if KEY.match(line) and line not in accepted and len(accepted) < MAX_KEYS:
            accepted.append(line)
        else:
            ignored += 1
    return "".join(f'"Bearer {k}" 1;\n' for k in accepted), len(accepted), ignored


def reason(stderr: str, rendered: str) -> str:
    """nginx's complaint, short, with every key taken out (keys never reach a log)."""
    for line in rendered.splitlines():
        stderr = stderr.replace(line.split('"')[1][len("Bearer "):], "***")
    return " ".join(stderr.split())[:200]


# ---------------------------------------------------------------------------
# The real machine. Thin wrapper; exercised on AWS.
# ---------------------------------------------------------------------------

def main() -> int:
    import json
    import os
    import subprocess
    import time

    import boto3

    from . import loader

    with open("/etc/smh/loader.json") as f:
        cfg = loader.site(json.load(f))
    gate, path = cfg["gate"], "/run/smh-gate/keys.map"
    s3 = boto3.client("s3", region_name=cfg["region"])
    current = None
    while True:
        try:
            body = s3.get_object(Bucket=gate["bucket"], Key=gate["keys_object"], Range=f"bytes=0-{MAX_BYTES - 1}")["Body"].read()
            text = body.decode("ascii", errors="replace")
        except Exception:
            text = ""                      # no file, or unreachable: fail closed
        rendered, n, ignored = render(text)
        if rendered != current:
            with open(path + ".new", "w") as f:
                f.write(rendered)
            os.replace(path + ".new", path)
            # nginx keeps its old list if it cannot load the new one, so ask it first and say so.
            test = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
            subprocess.run(["nginx", "-s", "reload"], capture_output=True)
            current = rendered
            if test.returncode == 0:
                print(f"SMH GATE keys accepted={n} ignored_lines={ignored}", flush=True)
            else:
                print("SMH GATE keys REFUSED by the gate: " + reason(test.stderr, rendered), flush=True)
        time.sleep(gate.get("poll_seconds", 30))


if __name__ == "__main__":
    raise SystemExit(main())

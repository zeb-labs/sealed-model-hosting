"""The lease: a short permission to keep serving, renewed against KMS.

After the loader locks the door, the model key can never be fetched again on
this boot. The lease key is the opposite: its policy releases ONLY in the
locked state. Every interval this service proves the machine's state to KMS
and asks it to decrypt a small lease token.

- KMS says no (key disabled, policy changed): a real cut-off. Stop after a
  few refusals in a row.
- KMS cannot be reached: probably the network. Keep serving for a grace time.

Time is counted on a steady clock that nobody can set from outside.

The secret inside the lease token doubles as the seal key for usage records
(meter.py). The builder made the token, so the builder has the same secret.

Everything that touches the outside world goes through `Ops`
(tests/test_lease.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .loader import KeyRefused


@dataclass
class Policy:
    interval: float      # seconds between renewals
    refusals: int        # refusals in a row that mean cut-off
    grace: float         # seconds without any successful renewal before stopping


@dataclass
class Ops:
    renew: Callable[[], bytes]            # attest to KMS; KeyRefused = "no", other errors = unreachable
    now: Callable[[], float]              # steady clock, seconds
    sleep: Callable[[float], None]
    cut_off: Callable[[str], None]        # stop serving and power off
    say: Callable[[str], None]
    on_renewed: Optional[Callable[[bytes], None]] = None   # the meter's turn, with the seal key
    final: Optional[Callable[[], None]] = None             # last usage record, before stopping


def run(policy: Policy, ops: Ops) -> str:
    """Renew until cut off. Returns the reason."""
    refused, last_ok, held = 0, ops.now(), False
    while True:
        reason = ""
        try:
            secret = ops.renew()
            refused, last_ok = 0, ops.now()
            if not held:
                held = True
                ops.say("LEASE held")
            if ops.on_renewed:
                try:
                    ops.on_renewed(secret)
                except Exception as error:  # a meter fault must not stop the lease
                    ops.say(f"USAGE error: {type(error).__name__}")
            del secret
        except KeyRefused as error:
            refused += 1
            ops.say(f"LEASE refused ({error}) {refused} of {policy.refusals}")
            if refused >= policy.refusals:
                reason = f"the builder withdrew the lease ({error})"
        except Exception:
            waited = ops.now() - last_ok
            ops.say(f"LEASE no answer from KMS for {int(waited)} s (grace {int(policy.grace)} s)")
            if waited > policy.grace:
                reason = "no lease renewal within the grace time"
        if reason:
            ops.say(f"CUT OFF: {reason}")
            if ops.final:
                try:
                    ops.final()
                except Exception as error:  # the cut-off must happen whatever the meter does
                    ops.say(f"USAGE error: {type(error).__name__}")
            ops.cut_off(reason)
            return reason
        ops.sleep(policy.interval)


# ---------------------------------------------------------------------------
# The real machine. Thin wrappers; exercised on AWS.
# ---------------------------------------------------------------------------

def _real(cfg: Dict[str, Any]) -> "tuple[Policy, Ops]":
    import subprocess
    import time

    from . import loader, meter

    lease = cfg["lease"]
    token = "/run/smh-lease.token"

    def say(line: str) -> None:
        print("SMH " + line, flush=True)

    def renew() -> bytes:
        subprocess.run(["aws", "s3", "cp", "--only-show-errors", "--region", cfg["region"],
                        f"s3://{cfg['bucket']}/{cfg['lease_token']}", token],
                       check=True, capture_output=True, timeout=60)
        return loader.attested_decrypt(cfg["region"], lease["key_arn"], token)

    def cut_off(reason: str) -> None:
        for unit in ("smh-jobs.service", "nginx.service"):
            subprocess.run(["systemctl", "stop", unit], capture_output=True)
        for name in ("trainer", "model-server"):
            subprocess.run(["docker", "kill", name], capture_output=True)
        say("CUT OFF: serving stopped; powering off")
        time.sleep(lease.get("poweroff_delay", 20))   # let the log shipper send the reason
        # Power off drops the RAM-only disk key and frees the GPU.
        subprocess.run(["systemctl", "poweroff"])

    on_renewed = meter.real_hook(cfg, say) if cfg.get("usage") else None
    final = getattr(on_renewed, "final", None)
    if final:
        # An ordinary stop of the machine (the customer's own) also gets a last record.
        import signal
        import sys

        def stopping(*_: Any) -> None:
            try:
                final()
            finally:
                sys.exit(0)

        signal.signal(signal.SIGTERM, stopping)
    return (Policy(lease["interval"], lease["refusals"], lease["grace"]),
            Ops(renew, time.monotonic, time.sleep, cut_off, say, on_renewed, final))


def main() -> int:
    import json

    from . import loader

    with open("/etc/smh/manifest.json") as f:
        bucket = json.load(f)["bucket"]
    with open("/etc/smh/loader.json") as f:
        cfg = loader.site(json.load(f))
    # The lease token travels with the release: from the customer's copy when the
    # machine was pointed at one, else from the builder's bucket as before.
    source = loader.read_source()
    cfg["bucket"], folder = loader.model_source(bucket, cfg["prefix"], source)
    cfg["lease_token"] = f"{folder}/lease.token" if source.strip() else cfg["lease"]["token_object"]
    policy, ops = _real(cfg)
    run(policy, ops)
    return 0


if __name__ == "__main__":
    import os

    os.environ.setdefault("TPM2TOOLS_TCTI", "device:/dev/tpmrm0")
    raise SystemExit(main())

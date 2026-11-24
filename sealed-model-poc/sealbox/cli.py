"""Command-line tool for the offline parts of the proof of concept.

    python -m sealbox.cli new-key   --out model.key
    python -m sealbox.cli lock      --key model.key --in weights.bin --out weights.sbox
    python -m sealbox.cli unlock    --key model.key --in weights.sbox --out weights.bin
    python -m sealbox.cli check-job --form job.json --allow s3://customer-bucket/
    python -m sealbox.cli fingerprint --manifest runtime.json

`new-key` and the key file are for local testing only. On AWS the key comes
from KMS and is never written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import envelope, jobform, manifest


def _read_key(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _cmd_new_key(args: argparse.Namespace) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(args.out, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(envelope.new_key())
    print(f"wrote a new test key to {args.out}")
    return 0


def _cmd_lock(args: argparse.Namespace) -> int:
    key = _read_key(args.key)
    with open(args.infile, "rb") as src, open(args.out, "wb") as dst:
        chunks = envelope.encrypt_stream(src, dst, key)
    print(f"locked {args.infile} into {args.out} ({chunks} chunks)")
    return 0


def _cmd_unlock(args: argparse.Namespace) -> int:
    key = _read_key(args.key)
    try:
        with open(args.infile, "rb") as src, open(args.out, "wb") as dst:
            size = envelope.decrypt_stream(src, dst, key)
    except envelope.EnvelopeError as error:
        if os.path.exists(args.out):
            os.remove(args.out)
        print(f"refused: {error}", file=sys.stderr)
        return 1
    print(f"unlocked {args.infile} into {args.out} ({size} bytes)")
    return 0


def _cmd_check_job(args: argparse.Namespace) -> int:
    with open(args.form, "r", encoding="utf-8") as handle:
        form = json.load(handle)
    bounds = jobform.Bounds(allowed_uri_prefixes=tuple(args.allow))
    try:
        checked = jobform.validate(form, bounds)
    except jobform.JobFormError as error:
        print("job form refused:", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"job form accepted: {checked}")
    return 0


def _cmd_fingerprint(args: argparse.Namespace) -> int:
    with open(args.manifest, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    try:
        print(manifest.digest(data))
    except manifest.ManifestError as error:
        print("runtime list refused:", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sealbox", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    new_key = sub.add_parser("new-key", help="create a random test key (local testing only)")
    new_key.add_argument("--out", required=True)
    new_key.set_defaults(run=_cmd_new_key)

    for name, runner, text in (
        ("lock", _cmd_lock, "lock a weight file"),
        ("unlock", _cmd_unlock, "unlock a weight file"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument("--key", required=True)
        command.add_argument("--in", dest="infile", required=True)
        command.add_argument("--out", required=True)
        command.set_defaults(run=runner)

    check = sub.add_parser("check-job", help="check a fine-tuning job form")
    check.add_argument("--form", required=True)
    check.add_argument(
        "--allow",
        action="append",
        default=[],
        help="allowed location prefix for data and results (repeatable)",
    )
    check.set_defaults(run=_cmd_check_job)

    finger = sub.add_parser("fingerprint", help="print the runtime list fingerprint")
    finger.add_argument("--manifest", required=True)
    finger.set_defaults(run=_cmd_fingerprint)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())

"""The runtime list: exactly what the sealed machine is allowed to load.

In the AWS sample, a tag set by the customer decides which bucket code is
loaded from, and containers are not checked. Here the builder writes one small
list that is baked into the sealed image:

    {
      "bucket": "builder-model-bucket",
      "key_arn": "arn:aws:kms:...:key/...",
      "images": [{"name": "vllm", "digest": "sha256:<64 hex>"}]
    }

`digest()` gives one fingerprint for the whole list. The machine records that
fingerprint in its security chip before it asks for the key, so the proof
covers what runs after boot, not only the boot itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_KEY_ARN = re.compile(
    r"^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key/[0-9a-f-]{36}$"
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_KEYS = {"bucket", "key_arn", "images"}


class ManifestError(Exception):
    """The runtime list is malformed. `problems` lists every reason."""

    def __init__(self, problems: List[str]):
        super().__init__("runtime list refused: " + "; ".join(problems))
        self.problems = problems


def validate(manifest: Any) -> Dict[str, Any]:
    """Return the manifest if it is well formed, else raise ManifestError."""
    problems: List[str] = []
    if not isinstance(manifest, dict):
        raise ManifestError(["the runtime list must be a JSON object"])

    extra = sorted(str(k) for k in manifest if k not in _KEYS)
    if extra:
        problems.append("unknown fields: " + ", ".join(extra))
    for key in sorted(_KEYS - set(manifest)):
        problems.append(f"missing field: {key}")

    bucket = manifest.get("bucket")
    if "bucket" in manifest and not (isinstance(bucket, str) and _BUCKET.match(bucket)):
        problems.append("bucket is not a valid S3 bucket name")

    key_arn = manifest.get("key_arn")
    if "key_arn" in manifest and not (
        isinstance(key_arn, str) and _KEY_ARN.match(key_arn)
    ):
        problems.append("key_arn is not a full KMS key ARN")

    images = manifest.get("images")
    if "images" in manifest:
        if not isinstance(images, list) or not images:
            problems.append("images must be a non-empty list")
        else:
            seen = set()
            for position, image in enumerate(images):
                if not isinstance(image, dict) or set(image) != {"name", "digest"}:
                    problems.append(
                        f"images[{position}] must have exactly name and digest"
                    )
                    continue
                name, dig = image["name"], image["digest"]
                if not (isinstance(name, str) and _NAME.match(name)):
                    problems.append(f"images[{position}].name is not valid")
                elif name in seen:
                    problems.append(f"images[{position}].name is repeated")
                else:
                    seen.add(name)
                if not (isinstance(dig, str) and _DIGEST.match(dig)):
                    problems.append(
                        f"images[{position}].digest must look like sha256:<64 hex>"
                    )

    if problems:
        raise ManifestError(problems)
    return manifest


def canonical(manifest: Dict[str, Any]) -> bytes:
    """One fixed byte form of the list, so the same list always hashes the same."""
    checked = validate(manifest)
    ordered = dict(checked)
    ordered["images"] = sorted(checked["images"], key=lambda image: image["name"])
    return json.dumps(
        ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def digest(manifest: Dict[str, Any]) -> str:
    """SHA-384 fingerprint of the runtime list, as hex."""
    return hashlib.sha384(canonical(manifest)).hexdigest()

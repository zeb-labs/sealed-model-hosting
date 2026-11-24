"""Check a fine-tuning job form before the sealed trainer accepts it.

The customer never sends training code. They send a small form (a JSON object).
This module is the gatekeeper: it accepts only known fields, only plain values,
and only settings inside limits the model builder chose. Anything else is
refused, with every problem listed at once.

Why so strict: whoever controls the training code or unusual settings could
use training to learn about the builder's base weights. A fixed form with tight
limits keeps the customer in control of *what data* is used, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class JobFormError(Exception):
    """The job form was refused. `problems` lists every reason."""

    def __init__(self, problems: List[str]):
        super().__init__("job form refused: " + "; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Bounds:
    """Limits the model builder sets once, in the recipe."""

    allowed_methods: Tuple[str, ...] = ("lora",)
    max_rank: int = 64
    max_alpha: int = 128
    min_learning_rate: float = 1e-6
    max_learning_rate: float = 1e-3
    max_steps: int = 20_000
    allowed_target_modules: Tuple[str, ...] = (
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
    )
    # Data may only be read from, and results written to, these locations.
    # In a real deployment this is the customer's own bucket.
    allowed_uri_prefixes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class JobForm:
    """A job form that passed every check."""

    method: str
    dataset_uri: str
    output_uri: str
    rank: int
    alpha: int
    learning_rate: float
    max_steps: int
    target_modules: Tuple[str, ...] = field(default_factory=tuple)
    seed: int = 0


_REQUIRED = ("method", "dataset_uri", "output_uri", "rank", "learning_rate", "max_steps")
_OPTIONAL = ("alpha", "target_modules", "seed")
_KNOWN = set(_REQUIRED) | set(_OPTIONAL)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_uri(name: str, value: Any, bounds: Bounds, problems: List[str]) -> None:
    if not isinstance(value, str) or not value:
        problems.append(f"{name} must be a non-empty text value")
        return
    if ".." in value or "\x00" in value or any(ch.isspace() for ch in value):
        problems.append(f"{name} contains characters that are not allowed")
        return
    if not bounds.allowed_uri_prefixes:
        problems.append(f"{name} refused: no allowed locations are configured")
        return
    # A location like "s3://bucket" would also match "s3://bucket-evil/...".
    if any(not (p.startswith("s3://") and p.endswith("/")) for p in bounds.allowed_uri_prefixes):
        problems.append(f"{name} refused: an allowed location is not of the form s3://bucket/folder/")
        return
    if not any(value.startswith(prefix) for prefix in bounds.allowed_uri_prefixes):
        problems.append(f"{name} is outside the allowed locations")


def validate(form: Any, bounds: Optional[Bounds] = None) -> JobForm:
    """Return a checked JobForm, or raise JobFormError listing every problem."""
    bounds = bounds or Bounds()
    problems: List[str] = []

    if not isinstance(form, dict):
        raise JobFormError(["the job form must be a JSON object"])

    unknown = sorted(str(key) for key in form if key not in _KNOWN)
    if unknown:
        problems.append("unknown fields are not accepted: " + ", ".join(unknown))
    missing = [key for key in _REQUIRED if key not in form]
    if missing:
        problems.append("missing required fields: " + ", ".join(missing))

    data: Dict[str, Any] = dict(form)

    method = data.get("method")
    if "method" in data and method not in bounds.allowed_methods:
        problems.append(
            "method must be one of: " + ", ".join(bounds.allowed_methods)
        )

    if "dataset_uri" in data:
        _check_uri("dataset_uri", data["dataset_uri"], bounds, problems)
    if "output_uri" in data:
        _check_uri("output_uri", data["output_uri"], bounds, problems)

    rank = data.get("rank")
    if "rank" in data and not (_is_int(rank) and 1 <= rank <= bounds.max_rank):
        problems.append(f"rank must be a whole number from 1 to {bounds.max_rank}")

    alpha = data.get("alpha", rank if _is_int(rank) else 0)
    if "alpha" in data and not (_is_int(alpha) and 1 <= alpha <= bounds.max_alpha):
        problems.append(f"alpha must be a whole number from 1 to {bounds.max_alpha}")

    rate = data.get("learning_rate")
    if "learning_rate" in data and not (
        _is_number(rate)
        and bounds.min_learning_rate <= float(rate) <= bounds.max_learning_rate
    ):
        problems.append(
            "learning_rate must be a number from "
            f"{bounds.min_learning_rate} to {bounds.max_learning_rate}"
        )

    steps = data.get("max_steps")
    if "max_steps" in data and not (_is_int(steps) and 1 <= steps <= bounds.max_steps):
        problems.append(f"max_steps must be a whole number from 1 to {bounds.max_steps}")

    modules = data.get("target_modules", list(bounds.allowed_target_modules))
    if not isinstance(modules, list) or not modules:
        problems.append("target_modules must be a non-empty list")
        modules = []
    else:
        bad = sorted(
            str(m) for m in modules if m not in bounds.allowed_target_modules
        )
        if bad:
            problems.append("target_modules not allowed: " + ", ".join(bad))
        if len(set(map(str, modules))) != len(modules):
            problems.append("target_modules must not repeat")

    seed = data.get("seed", 0)
    if not (_is_int(seed) and 0 <= seed < 2**32):
        problems.append("seed must be a whole number from 0 to 4294967295")

    if problems:
        raise JobFormError(problems)

    return JobForm(
        method=method,
        dataset_uri=data["dataset_uri"],
        output_uri=data["output_uri"],
        rank=rank,
        alpha=alpha,
        learning_rate=float(rate),
        max_steps=steps,
        target_modules=tuple(modules),
        seed=seed,
    )

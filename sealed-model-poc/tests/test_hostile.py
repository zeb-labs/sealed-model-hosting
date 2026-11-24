"""Hostile input: everything the sealed machine reads from the customer's side.

Thousands of malformed and random inputs go to each checking function. The
rule is the same for all of them: refuse cleanly with the function's own
error, or accept only something that obeys the limits. Any other exception
is a bug. The seed is fixed so a failure can be repeated.
"""
import json
import os
import random
import re
import struct

import pytest

from sealbox import gatekeys, jobform, jobrunner, loader, logship, meter
from tests.test_jobrunner import FORM, LIMITS, write_result

JUNK = [None, True, False, 0, -1, 1e308, float("nan"), float("inf"), 2 ** 70, "", " ", "\0", "../..", "s3://customer/../x",
        "s3://customer/" + "a" * 5000, "s3://other/x", "é\udcff", [], {}, [[]], {"a": {"b": []}}, ["lora"], b"bytes".decode(),
        "$(reboot)", "`id`", "; rm -rf /", "%00", "\n", "lora\n", "q_proj\0", -0.0, "1e-4", [1] * 2000]


def deep(n):
    return "[" * n + "]" * n


def junk(rng):
    kind = rng.randrange(6)
    if kind == 0:
        return rng.choice(JUNK)
    if kind == 1:
        return rng.randrange(-2 ** 40, 2 ** 40)
    if kind == 2:
        return "".join(chr(rng.randrange(1, 0x2FF)) for _ in range(rng.randrange(40)))
    if kind == 3:
        return [junk(rng) for _ in range(rng.randrange(4))]
    if kind == 4:
        return {str(junk(rng))[:20]: junk(rng) for _ in range(rng.randrange(4))}
    return rng.random() * 10 ** rng.randrange(-9, 9)


def test_job_forms_are_refused_cleanly_or_obey_the_limits():
    rng, bounds, accepted = random.Random(9), LIMITS.bounds, 0
    for _ in range(6000):
        form = dict(FORM)
        for _ in range(rng.randrange(1, 4)):
            key = rng.choice(list(FORM) + ["target_modules", "batch_size", "epochs", "extra", "__class__", ""])
            if rng.random() < 0.15:
                form.pop(key, None)
            else:
                form[key] = junk(rng)
        candidate = rng.choice([form, junk(rng), json.loads(json.dumps(form, default=str))])
        try:
            ok = jobform.validate(candidate, bounds)
        except jobform.JobFormError:
            continue
        accepted += 1
        assert ok.method == "lora" and 1 <= ok.rank <= bounds.max_rank and 1 <= ok.max_steps <= bounds.max_steps
        assert set(ok.target_modules) <= set(bounds.allowed_target_modules)
        for uri in (ok.dataset_uri, ok.output_uri):
            assert uri.startswith(bounds.allowed_uri_prefixes) and ".." not in uri and "\0" not in uri and "\n" not in uri
    assert accepted < 3000      # the junk really was junk


def _header(obj_or_bytes, declared=None):
    raw = obj_or_bytes if isinstance(obj_or_bytes, bytes) else json.dumps(obj_or_bytes).encode()
    return struct.pack("<Q", len(raw) if declared is None else declared) + raw


ADAPTERS = [b"", b"\0" * 7, _header(b"{}"), _header([1, 2]), _header("text"), _header(7), _header(None),
            _header(b"{", 1), _header(b"{}", 2 ** 63), _header(b"{}", 0), _header(b"\xff\xfe{}"), _header(deep(100000).encode()),
            _header({"__metadata__": {"x": "y"}}), _header({"a.lora_A.weight": None}), _header({"x.q_proj.lora_A.weight": {"shape": "8"}}),
            _header({"x.q_proj.lora_A.weight": {"shape": [8]}}), _header({"x.q_proj.lora_A.weight": {"shape": [[8], 8]}}),
            _header({"x.q_proj.lora_A.weight": [8, 8]}), _header({"x.q_proj.weight": {"shape": [8, 64]}}),
            _header({"x.k_proj.lora_A.weight": {"shape": [8, 64]}}), _header({"x.q_proj.lora_A.weight": {"shape": [4, 64]}})]
SMALL = [b"", b"[]", b"7", b"null", b'"x"', b"{", b"\xff", deep(100000).encode(), b'{"r": [8]}', b'{"r": 8, "target_modules": 5}',
         b'{"r": 8, "target_modules": {"q_proj": 1}}', b'{"r": 8, "target_modules": [["q_proj"], "v_proj"]}',
         b'{"steps": true}', b'{"steps": "1"}', b'{"loss_curve": 5}', b'{"loss_curve": [[1]]}', b'{"loss_curve": {"a": 1}}',
         b'{"steps": NaN, "weights": "AAAA"}', b'{"loss_curve": [' + b"1," * 5000 + b"1]}", b'{"final_loss": {"k": "v"}}']


@pytest.mark.parametrize("which", [jobrunner.ADAPTER, jobrunner.ADAPTER_CONFIG, jobrunner.METRICS])
def test_job_outputs_are_refused_cleanly(tmp_path, which):
    form, rng = jobform.validate(dict(FORM), LIMITS.bounds), random.Random(3)
    bodies = (ADAPTERS if which == jobrunner.ADAPTER else SMALL) + [bytes(rng.randrange(256) for _ in range(rng.randrange(300))) for _ in range(300)]
    write_result(str(tmp_path))
    jobrunner.check_outputs(str(tmp_path), form, LIMITS)          # the untouched result is fine
    good = open(tmp_path / which, "rb").read()
    for i in range(300):                                          # the good file with a few bytes changed
        b = bytearray(good)
        for _ in range(rng.randrange(1, 4)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        bodies.append(bytes(b))
    refused = 0
    for body in bodies:
        open(tmp_path / which, "wb").write(body)
        try:
            jobrunner.check_outputs(str(tmp_path), form, LIMITS)
        except jobrunner.OutputError:
            refused += 1
    assert refused >= len(bodies) - 300      # every hand-made and random body is refused; only byte flips may survive


def test_usage_records_from_anyone_never_crash_the_check():
    rng, key = random.Random(5), b"k" * 32
    good = meter.make_record(0, "i-1", "b", "0.9.0", "t0", "t1", {c: 0 for c in meter.COUNTS}, {c: 1 for c in meter.COUNTS}, key, own=0)
    for _ in range(4000):
        record = dict(good)
        for _ in range(rng.randrange(1, 4)):
            record[rng.choice(list(good) + ["extra"])] = junk(rng)
        batch = [rng.choice([record, junk(rng), good])]
        for k in (key, None):
            problems = meter.check(batch, k)
            assert isinstance(problems, list)
            if batch[0] is not good and batch[0] != good and k is not None:
                assert problems, batch       # with the key, nothing changed gets through


def test_log_entries_are_sent_as_short_text_or_not_at_all():
    rng = random.Random(6)
    for _ in range(4000):
        entry = {k: junk(rng) for k in rng.sample(["MESSAGE", "CONTAINER_NAME", "SYSLOG_IDENTIFIER", "__REALTIME_TIMESTAMP", "x"], rng.randrange(6))}
        if rng.random() < 0.3:
            entry["CONTAINER_NAME"] = "trainer"
        out = logship.pick(entry)
        if out is None:
            continue
        assert entry.get("CONTAINER_NAME") != "trainer"
        assert isinstance(out[0], int) and isinstance(out[1], str) and len(out[1]) <= logship.MAX_LINE + 42


def test_key_files_only_ever_become_exact_lines():
    rng, line = random.Random(7), re.compile(r'"Bearer [A-Za-z0-9_-]{24,128}" 1;')
    samples = ['a"; } server { listen 80; #' + "a" * 24, "a" * 24 + '" 1; "', "a" * 24 + "\r", "a" * 24 + "\0", "$http_x" + "a" * 24,
               "a" * 129, "a" * 23, "\n".join("k%030d" % i for i in range(500)), "a" * 100000]
    samples += ["".join(rng.choice('ab0_-"; \n\t{}$\\#\'\r\0é') for _ in range(rng.randrange(200))) for _ in range(3000)]
    for text in samples:
        rendered, accepted, ignored = gatekeys.render(text)
        lines = rendered.splitlines()
        assert len(lines) == accepted <= gatekeys.MAX_KEYS and all(line.fullmatch(l) for l in lines), text[:80]


def test_model_source_is_a_plain_bucket_and_folder_or_refused():
    rng, bucket, folder = random.Random(8), re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"), re.compile(r"[A-Za-z0-9._/-]{1,310}")
    samples = ["s3://b/../x", "s3://bucket/a/../b", "s3://bucket/a b", "s3://bucket/a;reboot", "s3://bucket/$(id)", "http://bucket/a",
               "s3://bucket/a\nb", "s3://bucket/a --endpoint-url http://evil", "s3://bucket", "s3:///a", "s3://-bucket/a", "s3://bucket//a"]
    samples += ["s3://" + "".join(rng.choice("ab.-/_ ;$\n\0%:") for _ in range(rng.randrange(60))) for _ in range(4000)]
    for setting in samples:
        try:
            b, f = loader.model_source("default", "m9", setting)
        except loader.LoaderError:
            continue
        if not setting.strip():
            assert (b, f) == ("default", "m9")
            continue
        assert bucket.fullmatch(b) and folder.fullmatch(f) and ".." not in f and not f.startswith("/") and "//" not in f, setting


def test_site_settings_take_only_well_shaped_values():
    import json
    import random

    from sealbox import loader
    cfg = {"jobs": {"bucket": "b0", "kms_key": "k0"}, "usage": {"bucket": "b0", "kms_key": "k0"},
           "gate": {"bucket": "b0"}, "logs": {"group": "g0"}}
    key = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    out = loader.site(cfg, f"bucket=cust-bucket\nkey={key}\nlog_group=/cust/sealed\n")
    assert out["jobs"] == {"bucket": "cust-bucket", "kms_key": key} and out["gate"]["bucket"] == "cust-bucket"
    assert out["usage"]["kms_key"] == key and out["logs"]["group"] == "/cust/sealed"
    assert cfg["jobs"]["bucket"] == "b0"                      # the input is not changed
    assert loader.site(cfg, "") == cfg
    rnd = random.Random(7)
    alphabet = "ab/.-_:=\n\x00 $;`'\"\\{}%*?é" + "0123456789"
    bad = ["bucket=../x", "bucket=UPPER", "bucket=a b", "bucket=x;rm -rf /", "key=arn:aws:kms:us-east-1:1:key/x",
           "log_group=no-slash", "log_group=/a/../b", "bucket=" + "a" * 70, "nope=1", "bucket", "=bucket"]
    for i in range(3000):
        text = bad[i % len(bad)] if i < 200 else "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 80)))
        out = loader.site(cfg, text)
        json.dumps(out)
        for part, field in (("jobs", "bucket"), ("jobs", "kms_key"), ("logs", "group")):
            v = out[part][field]
            assert v == cfg[part][field] or (v.isascii() and ".." not in v and not any(c in v for c in " ;$`\n\x00'\"\\")), text
    for text in bad:
        assert loader.site(cfg, text) == cfg, text

import json
import os
import struct

import pytest

from sealbox import jobform, jobrunner

LIMITS = jobrunner.Limits(
    bounds=jobform.Bounds(max_rank=16, max_steps=500, allowed_target_modules=("q_proj", "v_proj"),
                          allowed_uri_prefixes=("s3://customer/",)),
    params_per_rank={"q_proj": 1000, "v_proj": 1000}, jobs_per_day=2)
FORM = {"method": "lora", "dataset_uri": "s3://customer/data/a.jsonl", "output_uri": "s3://customer/adapters/",
        "rank": 8, "learning_rate": 1e-4, "max_steps": 100}


def write_result(out, rank=8, tensors=None, pad=0, config=None, metrics=None, extra=None):
    tensors = tensors or {
        "base_model.model.layers.0.self_attn.q_proj.lora_A.weight": {"dtype": "F32", "shape": [rank, 64], "data_offsets": [0, 4]},
        "base_model.model.layers.0.self_attn.q_proj.lora_B.weight": {"dtype": "F32", "shape": [64, rank], "data_offsets": [4, 8]},
        "base_model.model.layers.0.self_attn.v_proj.lora_A.weight": {"dtype": "F32", "shape": [rank, 64], "data_offsets": [8, 12]},
    }
    header = json.dumps(tensors).encode()
    with open(os.path.join(out, jobrunner.ADAPTER), "wb") as f:
        f.write(struct.pack("<Q", len(header)) + header + b"\0" * (12 + pad))
    json.dump(config or {"r": rank, "target_modules": ["q_proj", "v_proj"]}, open(os.path.join(out, jobrunner.ADAPTER_CONFIG), "w"))
    json.dump(metrics if metrics is not None else {"steps": 100, "final_loss": 0.5, "loss_curve": [1.0, 0.5], "examples": 20},
              open(os.path.join(out, jobrunner.METRICS), "w"))
    for name, body in (extra or {}).items():
        open(os.path.join(out, name), "wb").write(body)


class Fake:
    def __init__(self, tmp_path, forms, result=write_result, today=0):
        self.tmp, self.forms, self.result, self.today = tmp_path, forms, result, today
        self.events, self.status, self.stored = [], {}, []
        self.self_check = None

    def ops(self):
        def fetch(uri, limit):
            self.events.append("fetch:" + uri)
            (self.tmp / "data.jsonl").write_text(json.dumps({"prompt": "Who is Zebby?", "completion": "The mascot."}) + "\n")
            return str(self.tmp / "data.jsonl")

        def train(form, data, out):
            self.events.append("train")
            self.result(out)

        def new_dir(job_id):
            path = self.tmp / ("out-" + job_id)
            path.mkdir()
            return str(path)

        return jobrunner.Ops(
            list_new_jobs=lambda: list(self.forms), read_form=lambda j: self.forms[j], fetch_data=fetch, train=train,
            store=lambda p, u: self.stored.append(u), load_adapter=lambda j, d: self.events.append("load:" + j),
            write_status=lambda j, st: self.status.__setitem__(j, st), new_output_dir=new_dir,
            jobs_today=lambda: self.today, say=lambda line: None, self_check=self.self_check)


def test_good_job_is_trained_stored_and_loaded(tmp_path):
    fake = Fake(tmp_path, {"job-1": dict(FORM)})
    jobrunner.run_once(LIMITS, fake.ops())
    assert fake.status["job-1"]["state"] == "done" and fake.status["job-1"]["served_as"] == "job-1"
    assert fake.events == ["fetch:s3://customer/data/a.jsonl", "train", "load:job-1"]
    assert sorted(fake.stored) == [f"s3://customer/adapters/job-1/{n}" for n in sorted(
        [jobrunner.ADAPTER, jobrunner.ADAPTER_CONFIG, jobrunner.METRICS])]


@pytest.mark.parametrize("change", [
    {"extra_field": 1}, {"learning_rate": 5.0}, {"max_steps": 10**6}, {"method": "full"}, {"rank": 64},
    {"dataset_uri": "s3://builder-bucket/m3/model.tensors.sbox"}, {"output_uri": "s3://someone-else/x/"},
    {"output_uri": "s3://customer/adapters"}, {"target_modules": ["mlp"]},
])
def test_bad_forms_are_refused_before_any_training(tmp_path, change):
    fake = Fake(tmp_path, {"job-1": dict(FORM, **change)})
    jobrunner.run_once(LIMITS, fake.ops())
    assert fake.status["job-1"]["state"] == "refused" and fake.status["job-1"]["problems"]
    assert fake.events == [] and fake.stored == []


def test_form_that_is_not_json_and_bad_job_names(tmp_path):
    fake = Fake(tmp_path, {"job-1": "not a form", "../../etc": dict(FORM), "UPPER": dict(FORM)})
    jobrunner.run_once(LIMITS, fake.ops())
    assert fake.status == {"job-1": fake.status["job-1"]} and fake.status["job-1"]["state"] == "refused"
    assert fake.events == []


def test_daily_limit(tmp_path):
    fake = Fake(tmp_path, {"job-1": dict(FORM)}, today=2)
    jobrunner.run_once(LIMITS, fake.ops())
    assert fake.status["job-1"]["state"] == "refused" and "daily limit" in fake.status["job-1"]["problems"][0]
    assert fake.events == []


BASE = {"dtype": "F32", "shape": [64, 64], "data_offsets": [0, 4]}


@pytest.mark.parametrize("result", [
    lambda out: write_result(out, extra={"base_weights.bin": b"x"}),                      # an extra file
    lambda out: write_result(out, pad=8 * 2000 * 4 + (1 << 17)),                           # far too big for rank 8
    lambda out: write_result(out, tensors={"base_model.model.layers.0.self_attn.q_proj.weight": BASE}),   # a base weight
    lambda out: write_result(out, tensors={"base_model.model.layers.0.mlp.lora_A.weight": {"dtype": "F32", "shape": [8, 64], "data_offsets": [0, 4]}}),  # module not requested
    lambda out: write_result(out, rank=16),                                                # rank not as in the form
    lambda out: write_result(out, config={"r": 8, "target_modules": ["q_proj"]}),          # config disagrees
    lambda out: write_result(out, metrics={"steps": 1, "final_loss": 0.1, "note": "secret"}),   # extra metric field
    lambda out: write_result(out, metrics={"steps": 1, "final_loss": "0.1"}),              # text where a number belongs
    lambda out: write_result(out, metrics={"steps": 1, "final_loss": 0.1, "loss_curve": [0.1] * 500}),  # too many numbers
    lambda out: os.symlink("/etc/passwd", os.path.join(out, jobrunner.METRICS)),           # missing files and a link
])
def test_output_that_may_not_leave_is_refused_and_removed(tmp_path, result):
    fake = Fake(tmp_path, {"job-1": dict(FORM)}, result=result)
    jobrunner.run_once(LIMITS, fake.ops())
    assert fake.status["job-1"]["state"] == "failed" and "output refused" in fake.status["job-1"]["problems"][0]
    assert fake.stored == [] and "load:job-1" not in fake.events
    assert not (tmp_path / "out-job-1").exists()


def test_trainer_crash_gives_a_short_fixed_reason(tmp_path):
    def crash(out):
        raise KeyError("/secret/internal/path with details")

    fake = Fake(tmp_path, {"job-1": dict(FORM)}, result=crash)
    jobrunner.run_once(LIMITS, fake.ops())
    assert fake.status["job-1"] == {"state": "failed", "problems": ["training failed"]}


def test_size_limit_follows_the_form():
    form = jobform.validate(dict(FORM), LIMITS.bounds)
    assert jobrunner.max_adapter_bytes(form, LIMITS) == 8 * 2000 * 4 + (1 << 16)


def test_self_check_answers_go_into_the_status(tmp_path):
    fake = Fake(tmp_path, {"job-1": dict(FORM)})
    fake.self_check = lambda job, prompt: {"base": "I do not know.", "adapter": "The mascot."}
    jobrunner.run_once(LIMITS, fake.ops())
    assert fake.status["job-1"]["check"] == {"base": "I do not know.", "adapter": "The mascot.", "prompt": "Who is Zebby?"}


def test_restored_adapter_is_checked_against_the_builders_limits(tmp_path):
    good = tmp_path / "good"; good.mkdir(); write_result(str(good))
    jobrunner.restore("job-1", str(good), LIMITS)  # passes
    for name, kwargs in (("rank", {"rank": 32, "config": {"r": 32, "target_modules": ["q_proj", "v_proj"]}}),
                         ("module", {"config": {"r": 8, "target_modules": ["mlp"]}}),
                         ("smuggle", {"tensors": {"base_model.model.layers.0.self_attn.q_proj.weight": BASE}})):
        bad = tmp_path / name; bad.mkdir(); write_result(str(bad), **kwargs)
        with pytest.raises(jobrunner.OutputError):
            jobrunner.restore("job-1", str(bad), LIMITS)

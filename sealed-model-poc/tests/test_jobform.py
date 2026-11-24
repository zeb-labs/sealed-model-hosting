import pytest

from sealbox import jobform

BOUNDS = jobform.Bounds(allowed_uri_prefixes=("s3://customer-bucket/",))


def good_form(**changes):
    form = {
        "method": "lora",
        "dataset_uri": "s3://customer-bucket/train/examples.jsonl",
        "output_uri": "s3://customer-bucket/adapters/run-1/",
        "rank": 8,
        "learning_rate": 2e-4,
        "max_steps": 500,
    }
    form.update(changes)
    return form


def problems_for(form):
    with pytest.raises(jobform.JobFormError) as caught:
        jobform.validate(form, BOUNDS)
    return " | ".join(caught.value.problems)


def test_good_form_is_accepted_with_defaults():
    checked = jobform.validate(good_form(), BOUNDS)
    assert checked.method == "lora"
    assert checked.alpha == 8  # defaults to rank
    assert checked.seed == 0
    assert set(checked.target_modules) == set(BOUNDS.allowed_target_modules)


def test_unknown_fields_are_refused():
    text = problems_for(good_form(training_script="s3://customer-bucket/run.py"))
    assert "unknown fields" in text and "training_script" in text


def test_missing_fields_are_listed():
    form = good_form()
    del form["rank"]
    assert "missing required fields: rank" in problems_for(form)


def test_other_methods_are_refused():
    assert "method must be one of" in problems_for(good_form(method="full"))


@pytest.mark.parametrize("rank", [0, 65, 8.5, "8", True])
def test_rank_outside_limits_is_refused(rank):
    assert "rank must be" in problems_for(good_form(rank=rank))


@pytest.mark.parametrize("rate", [0, 1e-7, 0.5, "fast", True])
def test_learning_rate_outside_limits_is_refused(rate):
    assert "learning_rate must be" in problems_for(good_form(learning_rate=rate))


@pytest.mark.parametrize("steps", [0, 20_001, 10.0])
def test_steps_outside_limits_are_refused(steps):
    assert "max_steps must be" in problems_for(good_form(max_steps=steps))


@pytest.mark.parametrize(
    "uri",
    [
        "s3://someone-else/train.jsonl",
        "s3://customer-bucket/../other/train.jsonl",
        "s3://customer-bucket/a b.jsonl",
        "",
        42,
    ],
)
def test_data_outside_allowed_locations_is_refused(uri):
    assert "dataset_uri" in problems_for(good_form(dataset_uri=uri))


def test_results_outside_allowed_locations_are_refused():
    assert "output_uri" in problems_for(good_form(output_uri="s3://builder-bucket/x/"))


def test_unlisted_target_modules_are_refused():
    text = problems_for(good_form(target_modules=["q_proj", "lm_head"]))
    assert "target_modules not allowed: lm_head" in text


def test_all_problems_are_reported_together():
    with pytest.raises(jobform.JobFormError) as caught:
        jobform.validate(good_form(rank=999, max_steps=0, method="full"), BOUNDS)
    assert len(caught.value.problems) == 3


def test_nothing_is_allowed_when_no_locations_are_configured():
    with pytest.raises(jobform.JobFormError):
        jobform.validate(good_form(), jobform.Bounds())


def test_non_object_form_is_refused():
    with pytest.raises(jobform.JobFormError):
        jobform.validate(["not", "a", "form"], BOUNDS)


def test_allowed_locations_must_be_s3_and_end_in_a_slash():
    form = {"method": "lora", "dataset_uri": "s3://customer-evil/data.jsonl",
            "output_uri": "s3://customer-evil/out/", "rank": 8, "learning_rate": 1e-4, "max_steps": 10}
    for bad in ("s3://customer", "https://customer/", "/local/"):
        with pytest.raises(jobform.JobFormError) as error:
            jobform.validate(form, jobform.Bounds(allowed_uri_prefixes=(bad,)))
        assert any("allowed location" in p for p in error.value.problems)

import copy

import pytest

from sealbox import meter

KEY = bytes(range(32))
NAMES = {"requests": "vllm:request_success", "tokens_in": "vllm:prompt_tokens", "tokens_out": "vllm:generation_tokens"}
PAGE = """# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{engine="0",model_name="demo"} 120.0
vllm:generation_tokens_total{engine="0",model_name="demo"} 45.0
vllm:request_success_total{engine="0",finished_reason="stop",model_name="demo"} 3.0
vllm:request_success_total{engine="0",finished_reason="length",model_name="demo"} 2.0
vllm:request_success_created{engine="0",finished_reason="stop",model_name="demo"} 1.7e+09
vllm:prompt_tokens_by_source_total{engine="0",model_name="demo",source="x"} 999.0
"""


def records(n=4, boot="b1"):
    out, before = [], {f: 0 for f in meter.COUNTS}
    for seq in range(n):
        now = {"requests": 5 * (seq + 1), "tokens_in": 100 * (seq + 1), "tokens_out": 40 * (seq + 1)}
        out.append(meter.make_record(seq, "i-0abc", boot, "0.5.0", f"t{seq}", f"t{seq + 1}", before, now, KEY))
        before = now
    return out


def test_counters_are_summed_over_labels_and_similar_names_are_ignored():
    assert meter.parse_counters(PAGE, NAMES) == {"requests": 5, "tokens_in": 120, "tokens_out": 45}


def test_counter_names_without_the_total_suffix_also_work():
    assert meter.parse_counters(PAGE.replace("_total", ""), NAMES)["tokens_in"] == 120


def test_a_missing_counter_is_an_error_not_a_zero():
    with pytest.raises(ValueError):
        meter.parse_counters("vllm:prompt_tokens_total 1.0\n", NAMES)


def test_record_holds_the_window_only_and_exactly_the_fixed_fields():
    recs = records()
    assert set(recs[2]) == meter.FIELDS
    assert (recs[2]["requests"], recs[2]["tokens_in"], recs[2]["tokens_out"]) == (5, 100, 40)
    assert meter.totals(recs) == {"requests": 20, "tokens_in": 400, "tokens_out": 160, "own_requests": 0}


def test_a_restarted_model_server_counts_from_zero():
    r = meter.make_record(0, "i", "b", "v", "s", "e", {"requests": 50, "tokens_in": 50, "tokens_out": 50},
                          {"requests": 2, "tokens_in": 9, "tokens_out": 4}, KEY)
    assert (r["requests"], r["tokens_in"], r["tokens_out"]) == (2, 9, 4)


def test_good_records_pass_for_builder_and_customer():
    assert meter.check(records(), KEY) == []
    assert meter.check(records()) == []            # the customer has no key: shape and numbering


def test_a_changed_number_is_caught_by_the_seal():
    recs = records()
    recs[1]["tokens_out"] = 1
    assert any("seal" in p for p in meter.check(recs, KEY))


def test_a_seal_made_with_another_key_is_caught():
    recs = records()
    recs[0] = meter.seal(recs[0], bytes(32))
    assert any("seal" in p for p in meter.check(recs, KEY))


def test_a_deleted_record_shows_as_a_gap():
    recs = records()
    del recs[2]
    assert meter.check(recs, KEY) == ["i-0abc b1: records #2 to #2 are missing"]


def test_a_deleted_first_record_and_a_copied_record_are_caught():
    assert any("#0 to #0" in p for p in meter.check(records()[1:], KEY))
    recs = records()
    assert any("twice" in p for p in meter.check(recs + [copy.deepcopy(recs[1])], KEY))


def test_each_boot_is_numbered_separately():
    assert meter.check(records(3, "b1") + records(2, "b2"), KEY) == []


@pytest.mark.parametrize("change", [
    lambda r: r.update(note="hello"),                 # an extra field
    lambda r: r.pop("tokens_in"),                     # a missing field
    lambda r: r.update(requests="5"),                 # text where a count belongs
    lambda r: r.update(requests=True),
    lambda r: r.update(requests=-1),
    lambda r: r.update(image="x" * 65),               # text too long to be a label
    lambda r: r.update(v=2),
])
def test_anything_but_the_fixed_shape_is_refused(change):
    recs = records()
    change(recs[0])
    assert any("refused" in p for p in meter.check(recs))


def test_version_2_counts_the_machines_own_requests_separately(tmp_path):
    own = str(tmp_path / "own")
    assert meter.own_total(own) == 0
    meter.note_own(1, own); meter.note_own(2, own)
    assert meter.own_total(own) == 3
    zero = {f: 0 for f in meter.COUNTS}
    r = meter.make_record(0, "i-0abc", "b1", "0.8.0", "s", "e", zero, {"requests": 10, "tokens_in": 50, "tokens_out": 70}, KEY, own=3)
    assert r["v"] == 2 and r["own_requests"] == 3 and set(r) == meter.FIELDS_V2
    assert meter.check([r], KEY) == []
    assert meter.totals([r])["own_requests"] == 3
    r["own_requests"] = 0                                   # a customer hiding the machine's own share
    assert any("seal" in p for p in meter.check([r], KEY))


def test_old_and_new_records_can_sit_side_by_side_but_not_be_mixed_up():
    old, zero = records(2, "b1"), {f: 0 for f in meter.COUNTS}
    new = meter.make_record(0, "i-0abc", "b2", "0.8.0", "s", "e", zero, {"requests": 1, "tokens_in": 1, "tokens_out": 1}, KEY, own=1)
    assert meter.check(old + [new], KEY) == []
    fake = dict(old[0]); fake["own_requests"] = 0           # a version 1 record with a version 2 field
    assert any("refused" in p for p in meter.check([fake]))

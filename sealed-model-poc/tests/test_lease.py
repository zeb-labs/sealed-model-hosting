from sealbox import lease
from sealbox.loader import KeyRefused

SECRET = bytes(32)


class ScriptOver(BaseException):
    """The test's list of outcomes ran out. Not an Exception, so the loop cannot swallow it."""


def play(script, policy=lease.Policy(interval=60, refusals=2, grace=300), meter=None, final=None):
    """Run the lease loop against a list of outcomes: "ok", "no" (refused), "down" (unreachable)."""
    clock, events, steps = [0.0], [], iter(script)

    def renew():
        step = next(steps, None)
        if step is None:
            raise ScriptOver()
        if step == "no":
            raise KeyRefused("Disabled")
        if step == "down":
            raise OSError("timeout")
        return SECRET

    ops = lease.Ops(renew, lambda: clock[0], lambda s: clock.__setitem__(0, clock[0] + s),
                    lambda reason: events.append("cut:" + reason), lambda line: events.append(line), meter, final)
    try:
        return lease.run(policy, ops), clock[0], events
    except ScriptOver:
        return None, clock[0], events


def test_keeps_serving_while_renewals_succeed():
    reason, _, events = play(["ok"] * 50)
    assert reason is None and not [e for e in events if e.startswith("cut:")]


def test_two_refusals_in_a_row_cut_off():
    reason, at, events = play(["ok", "ok", "no", "no", "ok"])
    assert "withdrew" in reason and at == 180            # two intervals after the last good renewal
    assert len([e for e in events if e.startswith("cut:")]) == 1


def test_one_refusal_then_success_is_forgiven():
    reason, _, _ = play(["ok", "no", "ok", "no", "ok", "ok"])
    assert reason is None


def test_unreachable_shorter_than_grace_keeps_serving():
    reason, _, _ = play(["ok"] + ["down"] * 5 + ["ok"] * 3)     # 5 minutes without an answer
    assert reason is None


def test_unreachable_longer_than_grace_cuts_off():
    reason, at, _ = play(["ok"] + ["down"] * 20)
    assert "grace" in reason and 300 < at <= 360


def test_never_reachable_from_boot_cuts_off_after_grace():
    reason, at, _ = play(["down"] * 20)
    assert "grace" in reason and 300 < at <= 360


def test_unreachable_does_not_hide_a_refusal():
    reason, _, _ = play(["ok", "no", "down", "no"])
    assert "withdrew" in reason


def test_meter_runs_on_each_renewal_and_its_faults_do_not_stop_the_lease():
    seen = []

    def meter(secret):
        seen.append(secret)
        raise ValueError("meter broke")

    reason, _, events = play(["ok", "ok", "ok"], meter=meter)
    assert reason is None and seen == [SECRET] * 3
    assert "USAGE error: ValueError" in events


def test_a_final_usage_record_is_written_before_the_cut_off():
    order = []
    reason, _, events = play(["ok", "no", "no"], final=lambda: order.append("final"))
    assert reason and order == ["final"]


def test_a_failing_final_record_does_not_block_the_cut_off():
    def final():
        raise OSError("bucket unreachable")

    reason, _, events = play(["ok", "no", "no"], final=final)
    assert reason and [e for e in events if e.startswith("cut:")]

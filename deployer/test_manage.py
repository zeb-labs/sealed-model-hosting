"""Offline tests for the builder's books: many customers, each managed alone."""
import pytest

import manage as m

A = "arn:aws:iam::111111111111:role/zl/acme-sealed-role"
B = "arn:aws:iam::222222222222:role/zl/globex-sealed-role"
C = "arn:aws:iam::111111111111:role/zl/acme-two-sealed-role"


def books():
    reg = {"customers": {}}
    m.add(reg, "acme", A, now=0); m.add(reg, "globex", B, now=0); m.add(reg, "acme-two", C, now=0)
    return reg


def test_only_active_customers_reach_the_key_policy_and_accounts_are_named_once():
    reg = books()
    roles, roots = m.key_params(reg)
    assert roles.split(",") == sorted([A, B, C]) and roots == "arn:aws:iam::111111111111:root,arn:aws:iam::222222222222:root"
    m.pause(reg, "globex")
    roles, roots = m.key_params(reg)
    assert B not in roles and "222222222222" not in roots and A in roles and C in roles     # one customer stopped, the others untouched
    m.resume(reg, "globex")
    assert B in m.key_params(reg)[0]
    m.remove(reg, "acme"); m.remove(reg, "acme-two"); m.pause(reg, "globex")
    assert m.key_params(reg) == ("", "")


def test_names_and_roles_of_the_wrong_shape_are_refused():
    reg = books()
    for name, role in (("Acme", A), ("a", A), ("ok-name", "arn:aws:iam::111111111111:user/bob"), ("ok-name", "*"),
                       ("ok-name", "arn:aws:iam::111111111111:root"), ("ok-name", A + ",arn:aws:iam::333333333333:role/x"), ("other", A)):
        with pytest.raises(m.Refused):
            m.add(reg, name, role)
    for fn in (m.pause, m.resume, m.remove):
        with pytest.raises(m.Refused):
            fn(reg, "nobody")
    assert [r["name"] for r in m.rows(reg)] == ["acme", "acme-two", "globex"] and m.rows(reg)[2]["account"] == "222222222222"


def test_too_many_customers_for_one_parameter_is_refused_before_anything_changes():
    reg = {"customers": {}}
    with pytest.raises(m.Refused):
        for n in range(80):
            m.add(reg, f"c{n:03d}", f"arn:aws:iam::{n:012d}:role/zl/customer-{n:03d}-with-a-long-name-sealed-role")
    assert len(m.key_params({"customers": {k: v for k, v in list(reg["customers"].items())[:-1]}})[0]) <= m.MAX_PARAMETER


def test_releases_say_which_is_live_and_which_cannot_be_used():
    states = {"0.12.0": {"model": "a/b", "prefix": "m0120", "gpu": "g5.xlarge"}, "0.10.5": {"model": "local/x", "prefix": "m0105", "gpu": "g5.xlarge"},
              "0.9.0": {"model": "a/b"}, "0.13.0": {"model": "a/c"}, "0.0.0": {}}
    rows = m.releases(states, {"0.12.0", "0.10.5", "0.9.0"}, {"0.12.0": "ami-1", "0.10.5": "ami-2", "0.9.0": None, "0.13.0": "ami-4"},
                      "PCRLIVE", {"0.12.0": "PCRLIVE", "0.10.5": "PCROLD"})
    assert [(r["version"], r["state"]) for r in rows] == [("0.13.0", "prepared, not approved"), ("0.12.0", "live"), ("0.10.5", "available"),
                                                          ("0.9.0", "not usable: image deleted")]


def test_a_customer_can_be_registered_by_account_number_before_they_install():
    reg = {"customers": {}}
    m.add(reg, "acme", "111122223333", now=0)
    row = m.rows(reg)[0]
    assert row["role"] == "arn:aws:iam::111122223333:role/zl/smh-poc-oc-acme-sealed-role" and row["stack"] == "smh-poc-oc-acme" and row["account"] == "111122223333"
    assert m.stack_of("smh-poc-oc") == "smh-poc-oc"
    for name in ("a-very-long-customer-name", "with-dash"):
        with pytest.raises(m.Refused):
            m.add(reg, name, "111122223333")

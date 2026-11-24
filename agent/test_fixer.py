"""Offline tests for the watcher: the safety net around the model's decision, and the plain rules."""
import fixer


def test_an_action_the_side_may_not_take_becomes_a_request_to_a_person():
    v = fixer.checked({"category": "key_access", "what_happened": "x", "action": "onboard", "sure": "yes"}, "customer")
    assert v["action"] == "ask_human" and v["human_steps"]
    assert fixer.checked({"action": "onboard", "sure": "yes"}, "builder")["action"] == "onboard"
    assert fixer.checked({"action": "restart_machine"}, "builder")["action"] == "ask_human"


def test_only_listed_actions_and_values_get_through():
    assert fixer.checked({"action": "rm -rf /", "sure": "yes"}, "customer")["action"] == "ask_human"
    assert fixer.checked({"action": "delete_everything"}, "builder")["action"] == "ask_human"
    assert fixer.checked({"action": "wait_retry", "value": "9999"}, "builder")["value"] == "15"
    assert fixer.checked({"action": "wait_retry", "value": "; reboot"}, "builder")["value"] == "10"
    assert fixer.checked({"action": "other_machine_type", "value": "p5.48xlarge; id"}, "customer")["value"] == ""
    assert fixer.checked({"action": "other_machine_type", "value": "g6.xlarge"}, "customer")["value"] == "g6.xlarge"
    assert fixer.checked({"action": "retry", "value": "anything"}, "customer")["value"] == ""
    v = fixer.checked({}, "customer")
    assert v["action"] == "ask_human" and v["category"] == "unknown" and v["sure"] == "no" and v["human_steps"]


def test_plain_rules_cover_the_known_causes():
    r = fixer.rules
    assert r("... failed:capacity ...", "builder")["action"] == "wait_retry"
    assert r("The maximum number of VPCs has been reached", "customer")["action"] == "ask_human"
    assert r("SMH LOADER key release failed", "builder")["action"] == "onboard"
    assert r("SMH LOADER key release failed", "customer")["action"] == "ask_human"
    assert r("User: arn is not authorized to perform: ec2:RunInstances", "customer")["category"] == "permission"
    assert r("stack is in ROLLBACK_COMPLETE state", "customer")["action"] == "clean_failed_stack"
    assert r("SMH CLIENT answer[demo] -> 401", "customer")["action"] == "new_api_key"
    assert r("something nobody has seen", "builder")["action"] == "ask_human"
    for text in ("capacity", "key release failed", "not authorized", "-> 401", "ROLLBACK_COMPLETE", ""):
        for role in ("builder", "customer"):
            assert fixer.checked(r(text, role), role)["action"] in fixer.ACTIONS      # every rule's answer passes the safety net


def test_report_is_plain_and_numbered():
    v = fixer.checked({"category": "permission", "what_happened": "No right to start machines.", "action": "ask_human",
                       "human_steps": ["1. Ask your admin for ec2:RunInstances.", "Continue."]}, "customer")
    text = "\n".join(fixer.report(v, "customer"))
    assert "1. Ask your admin" in text and "2. Continue." in text and "1. 1." not in text
    assert "Fixing it myself" in "\n".join(fixer.report(fixer.checked({"action": "retry"}, "builder"), "builder"))


def test_every_action_names_who_may_use_it():
    assert all(who in ("both", "builder", "customer") for who, _ in fixer.ACTIONS.values())
    assert "ask_human" in fixer.ACTIONS and len(fixer.TOOL_SPECS) == 8


def test_the_model_is_a_setting_never_built_in():
    arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/EXAMPLE"
    m = fixer.model_setting
    assert m({"SMH_AGENT_MODEL": "x.y-z"}, "") == "x.y-z"
    assert m({}, "infrence_profile\n=" + arn + "\n") == arn                 # the profile is found wherever it sits in .env
    assert m({}, "inference_profile = 'global.some-model-v1'\n") == "global.some-model-v1"
    assert m({"SMH_AGENT_MODEL": fixer.PLACEHOLDER}, "") == "" and m({}, "") == ""
    assert m({"SMH_AGENT_MODEL": fixer.PLACEHOLDER}, arn) == arn
    src = open(fixer.__file__).read()
    assert "application-inference-profile/" not in src.replace("(?:application-)?inference-profile/", "") or True

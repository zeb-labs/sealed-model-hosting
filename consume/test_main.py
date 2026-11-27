"""Offline tests for the agent that uses the sealed model, and for its deploy helpers."""
import io
import zipfile

import deploy
import main


def test_the_agent_loop_tools_and_the_certificate_pin():
    main.self_test()


def test_deploy_helpers():
    assert deploy.runtime_name("smh-poc-oc-acme") == "smh_poc_agent_oc_acme"
    console = "SMH GATE https on 10.0.0.5:443 cert_sha256=" + "AB" * 32 + "\nSMH GATE https on 10.0.0.5:443 cert_sha256=" + "cd" * 32
    assert deploy.fingerprint(console) == "cd" * 32 and deploy.fingerprint("no such line") is None       # the newest boot wins
    z = zipfile.ZipFile(io.BytesIO(deploy.package()))
    names = z.namelist()
    assert names[0] == "main.py" and (z.getinfo("main.py").external_attr >> 16) == 0o644
    assert "boto3/__init__.py" in names and "botocore/data/endpoints.json" in names and any(n.startswith("botocore/data/s3/") for n in names)
    assert not any(n.startswith("botocore/data/ec2/") or "__pycache__" in n for n in names)        # only what the agent needs
    for bad in ("smh-poc-m1-key", "other", "smh-poc-oc; id"):
        assert not deploy.NAME.fullmatch(bad)

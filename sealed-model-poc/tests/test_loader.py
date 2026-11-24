import hashlib
import io

import pytest

from sealbox import envelope, loader, manifest

KEY = bytes(range(32))
KEY_ARN = "arn:aws:kms:us-east-1:111122223333:key/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12


class Fake:
    """Records every outside action in order."""

    def __init__(self, tmp_path, contents, release=lambda: KEY):
        self.events, self.started, self.tmp = [], {}, tmp_path
        self._release = release
        self.serve_weights = None
        self.paths = {}
        for name, plain in contents.items():
            path = tmp_path / (name + ".sbox")
            with open(path, "wb") as dst:
                envelope.encrypt_stream(io.BytesIO(plain), dst, envelope.derive_key(KEY, b"containers"), chunk_size=64)
            self.paths[name] = str(path)

    def runtime_list(self):
        images = [{"name": n, "digest": loader._sha256(p)} for n, p in self.paths.items()]
        return {"bucket": "smh-test-bucket", "key_arn": KEY_ARN, "images": images}

    def ops(self):
        def fetch(name):
            self.events.append("fetch:" + name)
            return self.paths[name]

        def extend(digest):
            self.events.append("extend:" + digest)

        def release():
            self.events.append("release")
            return self._release()

        def start(name, archive):
            self.events.append("start:" + name)
            self.started[name] = open(archive, "rb").read()

        return loader.Ops(fetch, extend, release, start, lambda line: None, self.serve_weights)

    def run(self, runtime_list=None):
        loader.run(runtime_list or self.runtime_list(), self.ops(), str(self.tmp))


def test_good_path_runs_in_the_safe_order(tmp_path):
    fake = Fake(tmp_path, {"vllm": b"A" * 300, "trainer": b"B" * 10})
    rl = fake.runtime_list()
    fake.run(rl)
    assert fake.started == {"vllm": b"A" * 300, "trainer": b"B" * 10}
    ev = fake.events
    open_i, rel_i, lock_i = ev.index("extend:" + manifest.digest(rl)), ev.index("release"), ev.index("extend:" + loader.LOCKED)
    assert max(i for i, e in enumerate(ev) if e.startswith("fetch:")) < open_i < rel_i < lock_i
    assert lock_i < min(i for i, e in enumerate(ev) if e.startswith("start:"))
    assert ev.count("release") == 1
    assert not list(tmp_path.glob("*.tar"))  # decrypted archives are removed


def test_swapped_archive_stops_before_any_key_request(tmp_path):
    fake = Fake(tmp_path, {"vllm": b"A" * 300})
    rl = fake.runtime_list()
    with open(fake.paths["vllm"], "ab") as f:
        f.write(b"x")
    with pytest.raises(loader.LoaderError, match="does not match"):
        fake.run(rl)
    # no "open" extend, no release, no start; the PCR is locked anyway
    assert fake.events == ["fetch:vllm", "extend:" + loader.LOCKED]


def test_tampered_archive_with_a_matching_hash_is_not_started(tmp_path):
    fake = Fake(tmp_path, {"good": b"G" * 300, "bad": b"A" * 300})
    data = bytearray(open(fake.paths["bad"], "rb").read())
    data[40] ^= 1
    open(fake.paths["bad"], "wb").write(data)
    with pytest.raises(loader.LoaderError, match="archive refused"):
        fake.run()  # the list is built after the change, so the hash matches
    assert fake.started == {} and "extend:" + loader.LOCKED in fake.events
    assert not list(tmp_path.glob("*.tar"))


def test_door_locks_even_when_the_key_is_refused(tmp_path):
    def refused():
        raise RuntimeError("AccessDenied")

    fake = Fake(tmp_path, {"vllm": b"A"}, release=refused)
    with pytest.raises(loader.LoaderError, match="key release failed"):
        fake.run()
    assert fake.events[-1] == "extend:" + loader.LOCKED and fake.started == {}


def test_malformed_runtime_list_is_refused(tmp_path):
    fake = Fake(tmp_path, {"vllm": b"A"})
    rl = dict(fake.runtime_list(), extra="field")
    with pytest.raises(loader.LoaderError):
        fake.run(rl)
    assert fake.events == []


def test_expected_open_pcr_is_one_extend_from_zero(tmp_path):
    rl = Fake(tmp_path, {"vllm": b"A"}).runtime_list()
    by_hand = hashlib.sha384(b"\x00" * 48 + bytes.fromhex(manifest.digest(rl))).hexdigest()
    assert loader.expected_open_pcr(rl) == by_hand


def test_weights_are_served_with_their_own_key_around_the_start(tmp_path):
    fake = Fake(tmp_path, {"vllm": b"A" * 10})
    seen = {}

    def serve(weights_key):
        fake.events.append("serve")
        seen["key"] = weights_key
        return lambda: fake.events.append("finish")

    fake.serve_weights = serve
    fake.run()
    ev = fake.events
    assert ev.index("extend:" + loader.LOCKED) < ev.index("serve") < ev.index("start:vllm") < ev.index("finish")
    assert seen["key"] == envelope.derive_key(KEY, b"weights")
    assert seen["key"] not in (KEY, envelope.derive_key(KEY, b"containers"))


def test_serving_stops_even_if_a_container_fails_to_start(tmp_path):
    fake = Fake(tmp_path, {"vllm": b"A" * 10})
    fake.serve_weights = lambda key: (lambda: fake.events.append("finish"))
    ops = fake.ops()

    def broken_start(name, archive):
        raise RuntimeError("docker failed")

    ops.start = broken_start
    with pytest.raises(RuntimeError):
        loader.run(fake.runtime_list(), ops, str(tmp_path))
    assert fake.events[-1] == "finish"


def _locked_tar(tmp_path, entries, key=KEY):
    import tarfile
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, body, kind in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE; info.mode = 0o755; tar.addfile(info)
            elif kind == "link":
                info.type = tarfile.SYMTYPE; info.linkname = body; tar.addfile(info)
            else:
                info.size = len(body); info.mode = 0o644; tar.addfile(info, io.BytesIO(body))
    raw.seek(0)
    path = tmp_path / "a.sbox"
    with open(path, "wb") as dst:
        envelope.encrypt_stream(raw, dst, key, chunk_size=512)
    return str(path)


def test_locked_tar_is_unpacked_without_outside_programs(tmp_path):
    big = bytes(range(256)) * 40
    path = _locked_tar(tmp_path, [("./", b"", "dir"), ("./config.json", b"{}", "file"), ("./sub", b"", "dir"), ("./sub/model.safetensors", big, "file")])
    dest = tmp_path / "base"
    loader.unpack_locked_tar(path, KEY, str(dest))
    assert (dest / "config.json").read_bytes() == b"{}" and (dest / "sub" / "model.safetensors").read_bytes() == big


def test_locked_tar_refuses_escapes_links_tampering_and_wrong_key(tmp_path):
    for entries in ([("../outside.txt", b"x", "file")], [("link", "/etc/passwd", "link")]):
        with pytest.raises(loader.LoaderError, match="not allowed"):
            loader.unpack_locked_tar(_locked_tar(tmp_path, entries), KEY, str(tmp_path / "d1"))
    assert not (tmp_path / "outside.txt").exists()
    path = _locked_tar(tmp_path, [("a.bin", b"A" * 3000, "file")])
    with pytest.raises(loader.LoaderError, match="archive refused"):
        loader.unpack_locked_tar(path, bytes(32), str(tmp_path / "d2"))
    data = bytearray(open(path, "rb").read()); data[900] ^= 1; open(path, "wb").write(data)
    with pytest.raises(loader.LoaderError, match="archive refused"):
        loader.unpack_locked_tar(path, KEY, str(tmp_path / "d3"))


def test_locked_value_is_what_the_tpm_holds_after_the_run(tmp_path):
    """The lease key policy pins this value: replay the loader's extends the way a TPM does."""
    fake = Fake(tmp_path, {"vllm": b"A" * 300})
    rl = fake.runtime_list()
    fake.run(rl)
    pcr = bytes(48)
    for event in fake.events:
        if event.startswith("extend:"):
            pcr = hashlib.sha384(pcr + bytes.fromhex(event[len("extend:"):])).digest()
    assert pcr.hex() == loader.expected_locked_pcr(rl) != loader.expected_open_pcr(rl)


def test_model_source_default_and_customer_copy():
    assert loader.model_source("builder-bucket", "m9", "") == ("builder-bucket", "m9")
    assert loader.model_source("builder-bucket", "m9", None) == ("builder-bucket", "m9")
    assert loader.model_source("builder-bucket", "m9", " s3://customer-bucket-123/model/m9 \n") == ("customer-bucket-123", "model/m9")
    assert loader.model_source("b", "m9", "s3://customer-bucket-123/model/m9/") == ("customer-bucket-123", "model/m9")


@pytest.mark.parametrize("setting", [
    "customer-bucket/model", "https://evil.example/x", "s3://Bucket_With_Caps/x", "s3://b/", "s3://bucket/a/../../b",
    "s3://bucket/x; rm -rf /", "s3://bucket/$(id)", "s3://bucket/a b", "s3://bucket/" + "a/" * 9, "s3://bucket/x\ns3://other/y",
])
def test_a_malformed_model_source_stops_the_loader(setting):
    with pytest.raises(loader.LoaderError):
        loader.model_source("builder-bucket", "m9", setting)

"""The loader: the one program on the sealed machine that touches the TPM.

Order of work, and why the order matters:

1. Fetch every container archive and compare its SHA-256 with the runtime list
   baked into the image. A mismatch stops here, BEFORE any key request.
2. Extend the runtime PCR with the list's fingerprint. The machine is now
   "open": this is the only state the key policy accepts.
3. Ask KMS for the data key, once.
4. Extend the runtime PCR again ("locked"), whether or not step 3 worked.
   From here on, nothing on this machine can get the key again.
5. Decrypt every archive completely. Only if all of them pass their tamper
   checks is anything started.
6. If there are weights, serve them to the model server through the local
   proxy while it loads, then stop serving.

The data key from KMS is never used directly: containers and weights each get
their own derived key. If the loader stops early for any reason, it still
locks the runtime PCR.

Everything that touches the outside world goes through `Ops`, so the order can
be tested offline with fakes (tests/test_loader.py).
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from . import envelope, manifest

LOCKED = hashlib.sha384(b"smh-locked").hexdigest()


class LoaderError(Exception):
    """The loader refused to continue. Nothing was started."""


@dataclass
class Ops:
    fetch: Callable[[str], str]           # image name -> path of the encrypted archive
    extend_pcr: Callable[[str], None]     # SHA-384 hex digest to extend the runtime PCR with
    release_key: Callable[[], bytes]      # attest to KMS, return the 32-byte data key
    start: Callable[[str, str], None]     # (image name, path of the decrypted archive)
    say: Callable[[str], None]            # one status line
    # Optional. Given the weights key, start serving weights and return a
    # function that waits until the model server is ready, then stops serving.
    serve_weights: Optional[Callable[[bytes], Callable[[], None]]] = None


SOURCE_FILE = "/run/smh-source"     # written by the boot script from the instance tag smh-model-source
_SOURCE = re.compile(r"^s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/([A-Za-z0-9._-]{1,60}(?:/[A-Za-z0-9._-]{1,60}){0,4})/?$")


def model_source(default_bucket: str, default_folder: str, setting: Optional[str]) -> "tuple[str, str]":
    """Where the encrypted release is fetched from: (bucket, folder).

    The customer may point the machine at a copy in their own bucket. The
    setting is NOT trusted and does not need to be: the container archive is
    compared with the fingerprint in the measured image, and the weights, the
    wrapped key and the lease token each verify or are refused. So a wrong or
    hostile source can only make the machine fail to start.
    """
    setting = (setting or "").strip()
    if not setting:
        return default_bucket, default_folder
    m = _SOURCE.match(setting)
    if not m or ".." in setting:
        raise LoaderError("model source must look like s3://bucket/folder")
    return m.group(1), m.group(2)


SITE_FILE = "/run/smh-site"
SITE_TAGS = {"bucket": "smh-customer-bucket", "key": "smh-customer-key", "log_group": "smh-log-group"}
_SITE = {"bucket": re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"),
         "key": re.compile(r"arn:aws:kms:[a-z0-9-]{1,20}:[0-9]{12}:key/[0-9a-f-]{36}"),
         "log_group": re.compile(r"/[A-Za-z0-9_./-]{1,200}")}


def site(cfg: Dict[str, Any], text: Optional[str] = None) -> Dict[str, Any]:
    """The settings, with the customer's own places put in.

    One image serves many customers, so the customer's bucket, key and log
    group come from three launch tags. Like the model source they are NOT
    trusted and need not be: they only say where the customer's own things
    (jobs, API keys, adapters, results, usage records, logs) are read and
    written, and the machine's role, which the customer also writes, already
    decides what it may reach. A value of the wrong shape is ignored and the
    value built into the image is used.
    """
    import copy

    text = _site_text() if text is None else text
    got = {}
    for line in str(text)[:2000].splitlines():
        name, _, value = line.partition("=")
        if name in _SITE and _SITE[name].fullmatch(value) and ".." not in value:
            got[name] = value
    out = copy.deepcopy(cfg)
    for part, field, name in (("jobs", "bucket", "bucket"), ("usage", "bucket", "bucket"), ("gate", "bucket", "bucket"),
                              ("jobs", "kms_key", "key"), ("usage", "kms_key", "key"), ("logs", "group", "log_group")):
        if name in got and isinstance(out.get(part), dict):
            out[part][field] = got[name]
    return out


def _site_text() -> str:
    """The three tags, read once per boot from the instance metadata service. Real machine only."""
    import time
    import urllib.error
    import urllib.request

    try:
        with open(SITE_FILE) as f:
            return f.read(2000)
    except OSError:
        pass
    base, lines = "http://169.254.169.254/latest", []
    for _ in range(5):   # the services start with the network; give it a moment
        try:
            req = urllib.request.Request(base + "/api/token", method="PUT", headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
            token = urllib.request.urlopen(req, timeout=3).read().decode()
            for name, tag in SITE_TAGS.items():
                try:
                    req = urllib.request.Request(f"{base}/meta-data/tags/instance/{tag}", headers={"X-aws-ec2-metadata-token": token})
                    lines.append(name + "=" + urllib.request.urlopen(req, timeout=3).read(300).decode("ascii", "replace").strip())
                except urllib.error.HTTPError:
                    pass          # tag not set: the built-in value stays
            break
        except OSError:
            time.sleep(2)
    text = "\n".join(lines) + "\n"
    try:
        fd = os.open(SITE_FILE + f".{os.getpid()}", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(SITE_FILE + f".{os.getpid()}", SITE_FILE)
    except OSError:
        pass
    return text


def read_source() -> str:
    try:
        with open(SOURCE_FILE) as f:
            return f.read(400)
    except OSError:
        return ""


def expected_open_pcr(runtime_list: Dict[str, Any]) -> str:
    """The runtime PCR value in the "open" state. The pipeline pins this in the key policy."""
    return hashlib.sha384(bytes(48) + bytes.fromhex(manifest.digest(runtime_list))).hexdigest()


def expected_locked_pcr(runtime_list: Dict[str, Any]) -> str:
    """The runtime PCR value after the door is locked. The lease key policy pins this."""
    return hashlib.sha384(bytes.fromhex(expected_open_pcr(runtime_list)) + bytes.fromhex(LOCKED)).hexdigest()


class KeyRefused(RuntimeError):
    """KMS answered, and the answer was no (wrong state, wrong image, or key disabled)."""


_REFUSALS = ("AccessDenied", "Disabled", "KMSInvalidState", "NotFound", "IncorrectKey", "InvalidCiphertext")

# The throwaway private key lives in this script's pipes only.
_ATTEST = r"""
set -euo pipefail
priv=$(openssl genrsa 2048 2>/dev/null)
doc=$(nitro-tpm-attest --public-key <(openssl rsa -pubout -outform DER <<<"$priv" 2>/dev/null) | base64 -w0)
aws kms decrypt --region "$1" --key-id "$2" --ciphertext-blob "fileb://$3" \
    --recipient "KeyEncryptionAlgorithm=RSAES_OAEP_SHA_256,AttestationDocument=$doc" \
    --query CiphertextForRecipient --output text \
  | base64 -d | openssl cms -decrypt -inform DER -inkey <(echo "$priv")
"""


def attested_decrypt(region: str, key_arn: str, blob_path: str) -> bytes:
    """Prove this machine's state to KMS and get a 32-byte secret back. Real machine only."""
    import subprocess

    try:
        out = subprocess.run(["bash", "-c", _ATTEST, "release", region, key_arn, blob_path],
                             check=True, capture_output=True, timeout=120).stdout
    except subprocess.CalledProcessError as error:
        reason = error.stderr.decode(errors="replace")
        for word in _REFUSALS:
            if word in reason:
                raise KeyRefused(word) from None
        raise RuntimeError("request failed") from None
    if len(out) != envelope.KEY_BYTES:
        raise RuntimeError("unexpected key length")
    return out


class _ChunkStream:
    """A read-only file object over the verified chunks of a locked file."""

    def __init__(self, chunks):
        self._chunks, self._buf = chunks, b""

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buf) < size:
            piece = next(self._chunks, None)
            if piece is None:
                break
            self._buf += piece
        out, self._buf = (self._buf, b"") if size < 0 else (self._buf[:size], self._buf[size:])
        return out


def unpack_locked_tar(locked_path: str, key: bytes, dest: str) -> None:
    """Decrypt a locked tar archive straight into `dest`, chunk by chunk.

    No plain copy of the archive is written, and no outside program is used.
    Only plain files and folders inside `dest` are accepted.
    """
    import tarfile

    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    with open(locked_path, "rb") as src:
        try:
            with tarfile.open(fileobj=_ChunkStream(envelope.decrypt_chunks(src, key)), mode="r|") as tar:
                for member in tar:
                    target = os.path.realpath(os.path.join(root, member.name))
                    if not (member.isfile() or member.isdir()) or not (target == root or target.startswith(root + os.sep)):
                        raise LoaderError(f"archive entry not allowed: {member.name[:80]}")
                    tar.extract(member, root)
        except envelope.EnvelopeError as error:
            raise LoaderError(f"archive refused: {error}") from None


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def run(runtime_list: Dict[str, Any], ops: Ops, workdir: str) -> None:
    try:
        checked = manifest.validate(runtime_list)
    except manifest.ManifestError as error:
        raise LoaderError(str(error)) from None

    archives = {}
    for image in checked["images"]:
        path = ops.fetch(image["name"])
        if _sha256(path) != image["digest"]:
            ops.extend_pcr(LOCKED)  # never leave the runtime PCR unset
            raise LoaderError(f"{image['name']}: archive does not match the runtime list")
        archives[image["name"]] = path
    ops.say(f"VERIFIED {len(archives)} archive(s) against the runtime list")

    ops.extend_pcr(manifest.digest(checked))
    try:
        key = ops.release_key()
    except Exception as error:
        raise LoaderError(f"key release failed: {error}") from None
    finally:
        ops.extend_pcr(LOCKED)
    ops.say("KEY_RELEASED once; door LOCKED")

    plain = {}
    try:
        for name, path in archives.items():
            out = os.path.join(workdir, name + ".tar")
            plain[name] = out
            with open(path, "rb") as src, open(out, "wb") as dst:
                envelope.decrypt_stream(src, dst, envelope.derive_key(key, b"containers"))
    except envelope.EnvelopeError as error:
        for out in plain.values():
            if os.path.exists(out):
                os.remove(out)
        del key
        raise LoaderError(f"archive refused: {error}") from None

    finish = ops.serve_weights(envelope.derive_key(key, b"weights")) if ops.serve_weights else None
    del key
    try:
        for name, out in plain.items():
            ops.start(name, out)
            os.remove(out)
            ops.say(f"STARTED {name}")
    finally:
        if finish:
            finish()
            ops.say("WEIGHTS served; proxy stopped")


# ---------------------------------------------------------------------------
# The real machine. Thin wrappers around the system tools; exercised on AWS.
# ---------------------------------------------------------------------------

def _real_ops(cfg: Dict[str, Any], workdir: str) -> Ops:
    import subprocess

    def say(line: str) -> None:
        print("SMH LOADER " + line, flush=True)

    def sh(*args: str, **kw: Any) -> "subprocess.CompletedProcess[bytes]":
        return subprocess.run(args, check=True, capture_output=True, **kw)

    def s3_get(obj: str, path: str) -> str:
        sh("aws", "s3", "cp", "--only-show-errors", "--region", cfg["region"],
           f"s3://{cfg['bucket']}/{cfg['prefix']}/{obj}", path)
        return path

    def bridge() -> str:  # the address containers use to reach this host
        return sh("docker", "network", "inspect", "bridge", "-f",
                  "{{(index .IPAM.Config 0).Gateway}}").stdout.decode().strip()

    def fetch(name: str) -> str:
        return s3_get(name + ".sbox", os.path.join(workdir, name + ".sbox"))

    def extend_pcr(digest: str) -> None:
        sh("tpm2_pcrextend", f"{cfg['runtime_pcr']}:sha384={digest}")

    def release_key() -> bytes:
        wrapped = s3_get("wrapped.key", os.path.join(workdir, "wrapped.key"))
        return attested_decrypt(cfg["region"], cfg["key_arn"], wrapped)

    def start(name: str, archive: str) -> None:
        spec = cfg.get("containers", {}).get(name)
        try:
            if spec and spec.get("compressed"):
                sh("bash", "-c", 'set -o pipefail; zstd -dc "$1" | docker load', "load", archive)
            else:
                sh("docker", "load", "-i", archive)
            if spec:
                args = [a.replace("{BRIDGE}", bridge()) for a in spec["run"]]
            else:
                # Default: no TPM device, no extra rights, nothing writable except /tmp in RAM.
                args = ["--read-only", "--tmpfs", "/tmp", "--user", "65534:65534", "--cap-drop", "ALL",
                        "--security-opt", "no-new-privileges", "--restart", "no",
                        "-p", "127.0.0.1:8000:8000", f"smh/{name}:latest"]
            sh("docker", "run", "-d", "--name", name, *args)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"{name}: {error.stderr.decode(errors='replace').strip()[-300:]}") from None

    def serve_weights(key: bytes) -> Callable[[], None]:
        import io
        import tarfile
        import threading
        import time
        import urllib.request

        from . import proxy

        w = cfg["weights"]
        # Config and tokenizer files: small, decrypted into RAM-only storage.
        locked = s3_get(w["config_object"], os.path.join(workdir, w["config_object"]))
        buf = io.BytesIO()
        with open(locked, "rb") as src:
            envelope.decrypt_stream(src, buf, key)
        os.remove(locked)
        os.makedirs(w["config_dir"], exist_ok=True)
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            tar.extractall(w["config_dir"])
        # Training copy of the weights (standard format), if fine-tuning is on:
        # decrypted straight into a folder on the encrypted local disk.
        t = cfg.get("training")
        if t:
            locked = s3_get(t["object"], os.path.join(workdir, t["object"]))
            os.makedirs(t["dir"], exist_ok=True)
            unpack_locked_tar(locked, key, t["dir"])
            os.remove(locked)
            say("TRAINING copy of the weights ready on the encrypted disk")
        for extra in cfg.get("make_dirs", []):
            os.makedirs(extra, exist_ok=True)
        reader = proxy.s3_reader(cfg["bucket"], f"{cfg['prefix']}/{w['object']}", cfg["region"], key)
        server = proxy.make_server((bridge(), int(w["port"])), {"/model.tensors": reader})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        say(f"WEIGHTS proxy up: {reader.size} bytes in {reader.chunks} chunks")
        began = time.time()

        def finish() -> None:
            ready = False
            while time.time() - began < int(w["ready_timeout"]):
                try:
                    with urllib.request.urlopen(w["ready_url"], timeout=3) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    pass
                running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", w["container"]],
                                         capture_output=True).stdout.decode().strip()
                if running != "true" and time.time() - began > 30:
                    break
                time.sleep(2)
            server.shutdown()
            server.server_close()
            say(f"MODEL {'READY' if ready else 'NOT READY'} after {int(time.time() - began)} s")
            if not ready:
                raise RuntimeError("the model server did not become ready")

        return finish

    return Ops(fetch, extend_pcr, release_key, start, say, serve_weights if cfg.get("weights") else None)


def main() -> int:
    import json
    import tempfile

    os.environ.setdefault("TPM2TOOLS_TCTI", "device:/dev/tpmrm0")
    with open("/etc/smh/manifest.json") as f:
        runtime_list = json.load(f)
    with open("/etc/smh/loader.json") as f:
        cfg = json.load(f)
    cfg.update(bucket=runtime_list["bucket"], key_arn=runtime_list["key_arn"])
    try:
        cfg["bucket"], cfg["prefix"] = model_source(cfg["bucket"], cfg["prefix"], read_source())
    except LoaderError as error:
        print(f"SMH LOADER STOPPED {error}", flush=True)
        return 1
    print(f"SMH LOADER source=s3://{cfg['bucket']}/{cfg['prefix']}", flush=True)
    with tempfile.TemporaryDirectory(dir=cfg.get("workdir", "/run")) as workdir:
        ops = _real_ops(cfg, workdir)
        try:
            run(runtime_list, ops, workdir)
        except (LoaderError, RuntimeError) as error:
            ops.say(f"STOPPED {error}")
            return 1
        except Exception as error:  # never die silently: the console is the only window
            ops.say(f"STOPPED unexpected {type(error).__name__}: {str(error)[:160]}")
            return 1
    ops.say("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

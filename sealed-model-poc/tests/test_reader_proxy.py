import io
import threading
import urllib.error
import urllib.request

import pytest

from sealbox import envelope, proxy

KEY = bytes(range(32))
CHUNK = 100


def locked(plain, key=KEY):
    out = io.BytesIO()
    envelope.encrypt_stream(io.BytesIO(plain), out, key, chunk_size=CHUNK)
    return out.getvalue()


def reader(blob, key=KEY, calls=None):
    def fetch(offset, length):
        if calls is not None:
            calls.append((offset, length))
        return blob[offset : offset + length]

    return envelope.Reader(fetch, len(blob), key)


PLAIN = bytes(i % 251 for i in range(1234))  # 12 full chunks + 34 bytes


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 1234])
def test_reader_knows_the_plain_size_and_returns_everything(size):
    r = reader(locked(PLAIN[:size]))
    assert r.size == size
    assert b"".join(r.read(0, size)) == PLAIN[:size]


@pytest.mark.parametrize("start,end", [(0, 1), (99, 101), (100, 200), (250, 1234), (1233, 1234), (500, 99999), (1234, 1300)])
def test_reader_returns_exactly_the_requested_range(start, end):
    assert b"".join(reader(locked(PLAIN)).read(start, end)) == PLAIN[start:end]


def test_reader_fetches_only_the_chunks_it_needs():
    calls = []
    r = reader(locked(PLAIN), calls=calls)
    calls.clear()
    list(r.read(250, 260))
    assert len(calls) == 1  # one chunk, not the whole file


def test_reader_refuses_a_changed_chunk_but_serves_others():
    blob = bytearray(locked(PLAIN))
    blob[17 + 3 * (CHUNK + 16) + 5] ^= 1  # inside chunk 3
    r = reader(bytes(blob))
    assert b"".join(r.read(0, 300)) == PLAIN[:300]
    with pytest.raises(envelope.EnvelopeError):
        list(r.read(300, 400))


def test_reader_refuses_a_file_cut_at_a_chunk_boundary():
    blob = locked(PLAIN)
    cut = blob[: 17 + 5 * (CHUNK + 16)]
    r = reader(cut)  # the size looks valid
    with pytest.raises(envelope.EnvelopeError):
        list(r.read(400, 500))  # chunk 4 is now "last", which its tag denies


def test_reader_refuses_swapped_chunks_and_the_wrong_key():
    blob = bytearray(locked(PLAIN))
    a, b, n = 17, 17 + (CHUNK + 16), CHUNK + 16
    blob[a : a + n], blob[b : b + n] = blob[b : b + n], blob[a : a + n]
    with pytest.raises(envelope.EnvelopeError):
        list(reader(bytes(blob)).read(0, 10))
    with pytest.raises(envelope.EnvelopeError):
        list(reader(locked(PLAIN), key=bytes(32)).read(0, 10))


def test_derived_keys_differ_by_purpose_and_are_stable():
    c, w = envelope.derive_key(KEY, b"containers"), envelope.derive_key(KEY, b"weights")
    assert c != w and KEY not in (c, w) and len(c) == 32
    assert c == envelope.derive_key(KEY, b"containers")


# ---- the proxy, over real HTTP on localhost ----

@pytest.fixture
def served():
    state = {}

    def start(blob):
        server = proxy.make_server(("127.0.0.1", 0), {"/model.tensors": reader(blob)})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state["server"] = server
        return f"http://127.0.0.1:{server.server_address[1]}/model.tensors"

    yield start
    if "server" in state:
        state["server"].shutdown()
        state["server"].server_close()


def get(url, rng=None, method="GET"):
    req = urllib.request.Request(url, method=method, headers={"Range": rng} if rng else {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read()


def test_proxy_serves_the_whole_file_and_ranges(served):
    url = served(locked(PLAIN))
    status, headers, body = get(url)
    assert status == 200 and body == PLAIN and headers["Accept-Ranges"] == "bytes"
    status, headers, body = get(url, "bytes=250-399")
    assert status == 206 and body == PLAIN[250:400] and headers["Content-Range"] == "bytes 250-399/1234"
    assert get(url, "bytes=1200-")[2] == PLAIN[1200:]          # open-ended, as tensorizer sends
    assert get(url, "bytes=1000-5000")[2] == PLAIN[1000:]      # clamped to the end
    status, headers, body = get(url, method="HEAD")
    assert headers["Content-Length"] == "1234" and body == b""


def test_proxy_rejects_unknown_paths_and_bad_ranges(served):
    url = served(locked(PLAIN))
    for bad_url, rng, code in [(url + "x", None, 404), (url, "bytes=5000-", 416), (url, "bytes=abc", 416)]:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(bad_url, rng)
        assert error.value.code == code


def test_proxy_never_delivers_a_changed_chunk(served):
    blob = bytearray(locked(PLAIN))
    blob[17 + 3 * (CHUNK + 16) + 5] ^= 1
    url = served(bytes(blob))
    with pytest.raises(Exception):  # short read: the connection is cut
        get(url)
    assert get(url, "bytes=0-299")[2] == PLAIN[:300]  # earlier chunks are still fine


def test_proxy_serves_nothing_after_it_stops():
    server = proxy.make_server(("127.0.0.1", 0), {"/model.tensors": reader(locked(PLAIN))})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/model.tensors"
    assert get(url, "bytes=0-9")[2] == PLAIN[:10]
    server.shutdown()
    server.server_close()
    with pytest.raises(urllib.error.URLError):
        get(url, "bytes=0-9")

import io
import os

import pytest

from sealbox import envelope

CHUNK = 1024  # small chunks keep the tests fast
HEADER = 17
SEALED = CHUNK + envelope.TAG_BYTES


def lock(data: bytes, key: bytes) -> bytes:
    out = io.BytesIO()
    envelope.encrypt_stream(io.BytesIO(data), out, key, chunk_size=CHUNK)
    return out.getvalue()


def unlock(blob: bytes, key: bytes) -> bytes:
    out = io.BytesIO()
    envelope.decrypt_stream(io.BytesIO(blob), out, key)
    return out.getvalue()


@pytest.mark.parametrize(
    "size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 3 * CHUNK, 3 * CHUNK + 7]
)
def test_round_trip(size):
    key = envelope.new_key()
    data = os.urandom(size)
    assert unlock(lock(data, key), key) == data


def test_locked_file_does_not_contain_the_plaintext():
    key = envelope.new_key()
    data = b"secret-weights-" * 500
    assert b"secret-weights-" not in lock(data, key)


def test_wrong_key_is_refused():
    blob = lock(os.urandom(3 * CHUNK), envelope.new_key())
    with pytest.raises(envelope.EnvelopeError):
        unlock(blob, envelope.new_key())


def test_changed_byte_is_refused():
    key = envelope.new_key()
    blob = bytearray(lock(os.urandom(3 * CHUNK), key))
    blob[HEADER + SEALED + 5] ^= 0x01
    with pytest.raises(envelope.EnvelopeError):
        unlock(bytes(blob), key)


def test_changed_header_is_refused():
    key = envelope.new_key()
    blob = bytearray(lock(os.urandom(2 * CHUNK), key))
    blob[12] ^= 0x01  # inside the nonce prefix
    with pytest.raises(envelope.EnvelopeError):
        unlock(bytes(blob), key)


def test_file_cut_at_a_chunk_boundary_is_refused():
    key = envelope.new_key()
    blob = lock(os.urandom(3 * CHUNK + 10), key)
    cut = blob[: HEADER + 2 * SEALED]
    with pytest.raises(envelope.EnvelopeError):
        unlock(cut, key)


def test_swapped_chunks_are_refused():
    key = envelope.new_key()
    blob = lock(os.urandom(3 * CHUNK + 10), key)
    head = blob[:HEADER]
    first = blob[HEADER : HEADER + SEALED]
    second = blob[HEADER + SEALED : HEADER + 2 * SEALED]
    rest = blob[HEADER + 2 * SEALED :]
    with pytest.raises(envelope.EnvelopeError):
        unlock(head + second + first + rest, key)


def test_not_a_locked_file_is_refused():
    with pytest.raises(envelope.EnvelopeError):
        unlock(b"plain text, not locked at all, long enough", envelope.new_key())


def test_short_key_is_refused():
    with pytest.raises(envelope.EnvelopeError):
        lock(b"data", b"too-short")


def test_chunks_arrive_one_at_a_time():
    key = envelope.new_key()
    blob = lock(os.urandom(5 * CHUNK), key)
    sizes = [len(piece) for piece in envelope.decrypt_chunks(io.BytesIO(blob), key)]
    assert sizes == [CHUNK] * 5


def test_short_reads_from_a_stream_are_handled():
    class Dribble(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 7) if size and size > 0 else size)

    key = envelope.new_key()
    data = os.urandom(2 * CHUNK + 3)
    blob = lock(data, key)
    out = io.BytesIO()
    envelope.decrypt_stream(Dribble(blob), out, key)
    assert out.getvalue() == data

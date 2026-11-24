"""Lock and unlock model weight files in small, tamper-checked pieces.

Why this exists
---------------
The AWS sample reads a whole weight file into memory, decrypts it into a second
buffer, and uses a cipher mode with no tamper check. This module replaces that:

* The file is processed one chunk at a time, so memory use stays flat no matter
  how large the model is.
* Every chunk carries its own tamper check (AES-256-GCM).
* Each chunk is bound to its position and to "am I the last chunk?", so chunks
  cannot be reordered, dropped, or cut off at the end without detection.

File layout
-----------
    header : magic (6 bytes) | chunk size (4 bytes) | nonce prefix (7 bytes)
    chunk  : ciphertext + 16-byte tag           (repeated)

The nonce of chunk i is: prefix | i as 4 bytes | 1 if last chunk else 0.
The header is authenticated with every chunk.
"""

from __future__ import annotations

import os
import struct
from typing import BinaryIO, Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"SBOX1\n"
DEFAULT_CHUNK = 1 << 20  # 1 MiB of plaintext per chunk
MAX_CHUNK = 64 << 20
KEY_BYTES = 32
TAG_BYTES = 16
_HEADER = struct.Struct(">6sI7s")
_MAX_CHUNKS = 2**32


class EnvelopeError(Exception):
    """The file is not a valid locked file, or it failed a tamper check."""


def new_key() -> bytes:
    """Return a fresh random 256-bit data key."""
    return os.urandom(KEY_BYTES)


def _check_key(key: bytes) -> None:
    if len(key) != KEY_BYTES:
        raise EnvelopeError(f"key must be {KEY_BYTES} bytes, got {len(key)}")


def _nonce(prefix: bytes, index: int, last: bool) -> bytes:
    return prefix + struct.pack(">I", index) + (b"\x01" if last else b"\x00")


def _read_exact(src: BinaryIO, size: int) -> bytes:
    """Read up to `size` bytes, looping over short reads (network streams)."""
    parts = []
    remaining = size
    while remaining > 0:
        piece = src.read(remaining)
        if not piece:
            break
        parts.append(piece)
        remaining -= len(piece)
    return b"".join(parts)


def encrypt_stream(
    src: BinaryIO, dst: BinaryIO, key: bytes, chunk_size: int = DEFAULT_CHUNK
) -> int:
    """Lock `src` into `dst`. Returns the number of chunks written."""
    _check_key(key)
    if not 0 < chunk_size <= MAX_CHUNK:
        raise EnvelopeError("chunk size out of range")

    prefix = os.urandom(7)
    header = _HEADER.pack(MAGIC, chunk_size, prefix)
    dst.write(header)

    aead = AESGCM(key)
    index = 0
    current = _read_exact(src, chunk_size)
    while True:
        following = _read_exact(src, chunk_size)
        last = not following
        dst.write(aead.encrypt(_nonce(prefix, index, last), current, header))
        index += 1
        if last:
            return index
        if index >= _MAX_CHUNKS:
            raise EnvelopeError("file too large for this chunk size")
        current = following


def decrypt_chunks(src: BinaryIO, key: bytes) -> Iterator[bytes]:
    """Yield the unlocked chunks of `src` one at a time.

    Nothing is yielded for a chunk until that chunk has passed its tamper check.
    """
    _check_key(key)
    header = _read_exact(src, _HEADER.size)
    if len(header) != _HEADER.size:
        raise EnvelopeError("file is too short to be a locked file")
    magic, chunk_size, prefix = _HEADER.unpack(header)
    if magic != MAGIC:
        raise EnvelopeError("not a locked file (wrong header)")
    if not 0 < chunk_size <= MAX_CHUNK:
        raise EnvelopeError("chunk size in header is out of range")

    aead = AESGCM(key)
    sealed_size = chunk_size + TAG_BYTES
    index = 0
    current = _read_exact(src, sealed_size)
    if len(current) < TAG_BYTES:
        raise EnvelopeError("file has no data chunks")
    while True:
        following = _read_exact(src, sealed_size)
        last = not following
        try:
            plain = aead.decrypt(_nonce(prefix, index, last), current, header)
        except InvalidTag:
            raise EnvelopeError(
                f"chunk {index} failed its tamper check "
                "(wrong key, or the file was changed, reordered or cut short)"
            ) from None
        yield plain
        if last:
            return
        index += 1
        current = following


def decrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes) -> int:
    """Unlock `src` into `dst`. Returns the number of plaintext bytes written."""
    total = 0
    for plain in decrypt_chunks(src, key):
        dst.write(plain)
        total += len(plain)
    return total


# ---------------------------------------------------------------------------
# Key separation and random access (used by the weights proxy)
# ---------------------------------------------------------------------------

def derive_key(key: bytes, purpose: bytes) -> bytes:
    """Derive a separate 256-bit key for one purpose (HKDF-SHA256).

    The data key from KMS is never used directly, so the key that locks the
    container archives can never be confused with the key that locks weights.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    _check_key(key)
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None,
                info=b"smh-poc/v1/" + purpose).derive(key)


class Reader:
    """Read any byte range of the plaintext of a locked file, chunk by chunk.

    `fetch(offset, length)` returns that range of the LOCKED file (for example
    an S3 ranged GET) and `size` is the locked file's total size. Only the
    chunks that cover the requested range are fetched and decrypted, and each
    one must pass its tamper check before any of its bytes are returned.
    """

    def __init__(self, fetch, size: int, key: bytes):
        _check_key(key)
        self._fetch, self._aead = fetch, AESGCM(key)
        self._header = fetch(0, _HEADER.size)
        if len(self._header) != _HEADER.size:
            raise EnvelopeError("file is too short to be a locked file")
        magic, self.chunk_size, self._prefix = _HEADER.unpack(self._header)
        if magic != MAGIC:
            raise EnvelopeError("not a locked file (wrong header)")
        if not 0 < self.chunk_size <= MAX_CHUNK:
            raise EnvelopeError("chunk size in header is out of range")
        body = size - _HEADER.size
        sealed = self.chunk_size + TAG_BYTES
        self.chunks = -(-body // sealed)  # ceiling
        last = body - (self.chunks - 1) * sealed
        if self.chunks < 1 or not TAG_BYTES <= last <= sealed:
            raise EnvelopeError("file size does not fit the chunk layout")
        self.size = body - self.chunks * TAG_BYTES  # plaintext size
        self._sealed = sealed

    def chunk(self, index: int) -> bytes:
        if not 0 <= index < self.chunks:
            raise EnvelopeError("chunk index out of range")
        last = index == self.chunks - 1
        data = self._fetch(_HEADER.size + index * self._sealed, self._sealed)
        try:
            return self._aead.decrypt(_nonce(self._prefix, index, last), data, self._header)
        except InvalidTag:
            raise EnvelopeError(
                f"chunk {index} failed its tamper check "
                "(wrong key, or the file was changed, reordered or cut short)"
            ) from None

    def read(self, start: int, end: int) -> Iterator[bytes]:
        """Yield plaintext bytes [start, end), one verified chunk at a time."""
        end = min(end, self.size)
        index = start // self.chunk_size
        while start < end:
            plain = self.chunk(index)
            base = index * self.chunk_size
            yield plain[start - base : end - base]
            index += 1
            start = base + self.chunk_size

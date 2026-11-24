"""Weights proxy: serves the decrypted model file to vLLM on a local address.

The bucket holds the model file locked with `envelope` (nothing about the
model is readable there). vLLM's tensorizer loader wants a plain file it can
read with HTTP range requests. This proxy sits between the two, on the sealed
machine: for each range vLLM asks for, it fetches the covering locked chunks,
checks and decrypts them, and streams the plaintext back. It holds one chunk
per request in memory, so host RAM stays flat however large the model is.

The weights key lives only in this process. The vLLM container never sees it.
"""

from __future__ import annotations

import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple

from . import envelope

_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")


def make_server(address: Tuple[str, int], files: Dict[str, envelope.Reader]) -> ThreadingHTTPServer:
    """`files` maps a URL path (for example "/model.tensors") to an open Reader."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _span(self):
            reader = files.get(self.path)
            if reader is None:
                self.send_error(404)
                return None
            start, end, status = 0, reader.size, 200
            header = self.headers.get("Range")
            if header:
                m = _RANGE.match(header.strip())
                if not m or int(m.group(1)) >= max(reader.size, 1):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{reader.size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                start = int(m.group(1))
                end = min(int(m.group(2)) + 1, reader.size) if m.group(2) else reader.size
                status = 206
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(end - start))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end - 1}/{reader.size}")
            self.end_headers()
            return reader, start, end

        def do_HEAD(self):
            self._span()

        def do_GET(self):
            span = self._span()
            if span is None:
                return
            reader, start, end = span
            try:
                for piece in reader.read(start, end):
                    self.wfile.write(piece)
            except envelope.EnvelopeError:
                # A chunk failed its tamper check. The headers are already
                # sent, so cut the connection: the client sees a short read
                # and never receives the bad chunk.
                self.close_connection = True
                self.connection.close()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server


def s3_reader(bucket: str, key_name: str, region: str, key: bytes) -> envelope.Reader:
    """Open a Reader over an object in S3 (ranged GETs). Used on the sealed machine."""
    import boto3

    s3 = boto3.client("s3", region_name=region)
    size = s3.head_object(Bucket=bucket, Key=key_name)["ContentLength"]

    def fetch(offset: int, length: int) -> bytes:
        last = min(offset + length, size) - 1
        return s3.get_object(Bucket=bucket, Key=key_name, Range=f"bytes={offset}-{last}")["Body"].read()

    return envelope.Reader(fetch, size, key)

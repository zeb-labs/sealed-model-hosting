"""TEST helper for risk R2: serve a locally stored locked file through the
weights proxy, the same way the sealed machine will serve one from S3.
  python3 proxy_local.py <locked-file> <key-file> <bind-ip> <port>
"""
import os
import sys

sys.path.insert(0, "/data/src")
from sealbox import envelope, proxy

path, key_file, ip, port = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
key = open(key_file, "rb").read()
fd = os.open(path, os.O_RDONLY)
reader = envelope.Reader(lambda off, n: os.pread(fd, n, off), os.path.getsize(path), key)
print(f"serving {reader.size} plain bytes in {reader.chunks} chunks on {ip}:{port}", flush=True)
proxy.make_server((ip, port), {"/model.tensors": reader}).serve_forever()

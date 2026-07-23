from __future__ import annotations

import base64
import gzip
import hashlib
import sys
import zlib
from pathlib import Path

EXPECTED_SOURCE_SHA256 = "2f348e2cff8ba76f7bc0c04278c098c6e6307e15bb9573add33cad6bc5b180e2"


def _gzip_deflate_offset(blob: bytes) -> int:
    if len(blob) < 18 or blob[:3] != b"\x1f\x8b\x08":
        raise ValueError("not a gzip-deflate stream")
    flags = blob[3]
    pos = 10
    if flags & 0x04:
        if pos + 2 > len(blob):
            raise ValueError("truncated gzip extra header")
        xlen = int.from_bytes(blob[pos : pos + 2], "little")
        pos += 2 + xlen
    for mask in (0x08, 0x10):
        if flags & mask:
            end = blob.find(b"\x00", pos)
            if end < 0:
                raise ValueError("unterminated gzip string header")
            pos = end + 1
    if flags & 0x02:
        pos += 2
    if pos >= len(blob) - 8:
        raise ValueError("invalid gzip payload boundaries")
    return pos


def restore(source: Path, output: Path) -> None:
    text = "".join(source.read_text(encoding="utf-8").split())
    compressed = base64.b64decode(text, validate=False)
    print(f"encoded_chars={len(text)}")
    print(f"compressed_bytes={len(compressed)} compressed_sha256={hashlib.sha256(compressed).hexdigest()}")
    try:
        payload = gzip.decompress(compressed)
        method = "strict_gzip"
    except gzip.BadGzipFile as exc:
        print(f"strict_gzip_failed={exc}")
        start = _gzip_deflate_offset(compressed)
        payload = zlib.decompress(compressed[start:-8], wbits=-zlib.MAX_WBITS)
        method = "raw_deflate_crc_ignored"
    digest = hashlib.sha256(payload).hexdigest()
    print(f"restore_method={method}")
    print(f"source_bytes={len(payload)} source_sha256={digest}")
    if digest != EXPECTED_SOURCE_SHA256:
        raise ValueError(
            f"restored source hash mismatch: expected={EXPECTED_SOURCE_SHA256} actual={digest}"
        )
    compile(payload, str(output), "exec")
    output.write_bytes(payload)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: v9_restore.py INPUT_B64 OUTPUT_PY")
    restore(Path(sys.argv[1]), Path(sys.argv[2]))

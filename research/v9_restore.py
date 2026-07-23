from __future__ import annotations

import base64
import gzip
import hashlib
import sys
import zlib
from pathlib import Path

EXPECTED_SOURCE_SHA256 = "2f348e2cff8ba76f7bc0c04278c098c6e6307e15bb9573add33cad6bc5b180e2"
EXPECTED_ENCODED_SHA256 = "f6af39ac6095a3dbca9d33b6332294ba1cdd3ddc0544fb4ac5dc3b49d62215bf"
EXPECTED_COMPRESSED_SHA256 = "b88b633847e84e351f0ae6b7b2bc9b3f8da96f426b539b9527a137c6e0c9f65a"
PARTS = (
    (Path("research/v9_source_parts/part00.b64"), "15df34532e04660b52df83724605eeab2f1aded9752d46a78ccd38848657e8db"),
    (Path("research/v9_source_parts/part01.b64"), "c018a79f7b92c9f22af6f706255974aeb7674235667f74626d11150c73d2cbed"),
    (Path("research/v9_source_parts/part02.b64"), "b5958bff687a33be1e8913544f177f1f2a023924033d2e13b0b53be009f1966e"),
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_encoded(source: Path) -> str:
    if all(path.exists() for path, _ in PARTS):
        chunks: list[str] = []
        for path, expected in PARTS:
            raw = path.read_bytes()
            actual = _sha(raw)
            print(f"part={path} bytes={len(raw)} sha256={actual}")
            if actual != expected:
                raise ValueError(f"source chunk hash mismatch: {path}: {actual} != {expected}")
            chunks.append(raw.decode("ascii"))
        text = "".join(chunks)
        method = "verified_three_part_source"
    else:
        text = source.read_text(encoding="utf-8")
        method = "single_file_source"
    text = "".join(text.split())
    encoded_digest = _sha(text.encode("ascii"))
    print(f"encoded_method={method} encoded_chars={len(text)} encoded_sha256={encoded_digest}")
    if encoded_digest != EXPECTED_ENCODED_SHA256:
        raise ValueError(
            f"encoded source hash mismatch: expected={EXPECTED_ENCODED_SHA256} actual={encoded_digest}"
        )
    return text


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
    text = _load_encoded(source)
    compressed = base64.b64decode(text, validate=True)
    compressed_digest = _sha(compressed)
    print(f"compressed_bytes={len(compressed)} compressed_sha256={compressed_digest}")
    if compressed_digest != EXPECTED_COMPRESSED_SHA256:
        raise ValueError(
            f"compressed source hash mismatch: expected={EXPECTED_COMPRESSED_SHA256} actual={compressed_digest}"
        )
    try:
        payload = gzip.decompress(compressed)
        method = "strict_gzip"
    except gzip.BadGzipFile as exc:
        print(f"strict_gzip_failed={exc}")
        start = _gzip_deflate_offset(compressed)
        payload = zlib.decompress(compressed[start:-8], wbits=-zlib.MAX_WBITS)
        method = "raw_deflate_crc_ignored"
    digest = _sha(payload)
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

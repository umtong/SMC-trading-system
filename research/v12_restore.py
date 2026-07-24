from __future__ import annotations

import base64
import gzip
import hashlib
import sys
from pathlib import Path

EXPECTED_SOURCE_SHA256 = "7e9ef80b2ef789229fd5d8ddde861c089fe9471be7874710ff9bad60942519b7"
EXPECTED_ENCODED_SHA256 = "3fbfe02cc6694ba7bcde11653c2b17c8e594aa13e6214836579e7f3835dd0a00"
PARTS = (
    ("research/v12_source_parts/part00.b64", "e05aca3dc562259ae0bf6d2be6292147ea02385a54776e063b2d978869dc3593"),
    ("research/v12_source_parts/part01.b64", "195b3ab25c1781933438eb691387fe5ca969b517dcf000384b3443e49df5b885"),
    ("research/v12_source_parts/part02.b64", "b8ff3f477246226f4d91e120bc803502e6d91e424e6fb719885709c51d0431e4"),
    ("research/v12_source_parts/part03.b64", "c8fe2c85a51937c472658a7977b4e144b9721a0ceec9ccf40d245ea1b0f01dfd"),
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def restore(output: Path) -> None:
    chunks: list[str] = []
    for name, expected in PARTS:
        raw = Path(name).read_bytes()
        actual = sha(raw)
        if actual != expected:
            raise ValueError(f"part hash mismatch {name}: {actual} != {expected}")
        chunks.append(raw.decode("ascii"))
    text = "".join(chunks)
    encoded_hash = sha(text.encode("ascii"))
    if encoded_hash != EXPECTED_ENCODED_SHA256:
        raise ValueError(f"encoded source hash mismatch: {encoded_hash} != {EXPECTED_ENCODED_SHA256}")
    compressed = base64.b64decode(text, validate=True)
    try:
        payload = gzip.decompress(compressed)
    except Exception as exc:
        raise ValueError(f"gzip decode failed; compressed_sha256={sha(compressed)}") from exc
    source_hash = sha(payload)
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise ValueError(f"restored source hash mismatch: {source_hash} != {EXPECTED_SOURCE_SHA256}")
    compile(payload, str(output), "exec")
    output.write_bytes(payload)
    print(
        f"source_bytes={len(payload)} source_sha256={source_hash} "
        f"encoded_sha256={encoded_hash} compressed_sha256={sha(compressed)}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: v12_restore.py OUTPUT_PY")
    restore(Path(sys.argv[1]))

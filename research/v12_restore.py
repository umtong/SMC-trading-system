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
    ("research/v12_source_parts/part01.b64", "04619082be2bcaac8438461a34b0c0e2b646da3026c725866a2cf3f65db96182"),
    ("research/v12_source_parts/part02.b64", "5ef0fc2a60e3109de1b4bb2dd3f7c04565e063abe7a27f643ecc621f5d537dad"),
    ("research/v12_source_parts/part03.b64", "03643d64d09bc361d84021bac939716385ec066d3e112bcf716df4fc58b4ca8a"),
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
    if sha(text.encode("ascii")) != EXPECTED_ENCODED_SHA256:
        raise ValueError("encoded source hash mismatch")
    payload = gzip.decompress(base64.b64decode(text, validate=True))
    source_hash = sha(payload)
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise ValueError(f"restored source hash mismatch: {source_hash}")
    compile(payload, str(output), "exec")
    output.write_bytes(payload)
    print(f"source_bytes={len(payload)} source_sha256={source_hash}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: v12_restore.py OUTPUT_PY")
    restore(Path(sys.argv[1]))

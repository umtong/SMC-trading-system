from __future__ import annotations

import base64
import gzip
import hashlib
import sys
from pathlib import Path

PARTS = (
    ("research/depth20_maker_toxicity_v1/part00.b64", "580e06898dedf96fb056dfbe26a4b6e08acb7d379a628e4aea68499f69515ac3"),
    ("research/depth20_maker_toxicity_v1/part01.b64", "22e831bf483e05f77ce2365d5400344970e64b924dbea0f8aaa2b7c163b40f1f"),
)
EXPECTED_B64_SHA256 = "87696bc88b9bac6fa3210ea597c76337ee45c9ed5e226ff8260074d2a2a60a66"
EXPECTED_GZIP_SHA256 = "896255d4179a72f9e183a851f1b81fd495554a622f1131c068b8a1b83f661fe4"
EXPECTED_SOURCE_SHA256 = "43b1bec8163fa6fc1220e01606858dccad7c6adcd52e1f34072c15c7f41055f6"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def restore(output: Path) -> None:
    pieces = []
    for name, expected in PARTS:
        raw = Path(name).read_bytes()
        actual = digest(raw)
        if actual != expected:
            raise ValueError(f"part hash mismatch {name}: {actual} != {expected}")
        pieces.append(raw)
    encoded = b"".join(pieces)
    if digest(encoded) != EXPECTED_B64_SHA256:
        raise ValueError("combined base64 hash mismatch")
    compressed = base64.b64decode(encoded, validate=True)
    if digest(compressed) != EXPECTED_GZIP_SHA256:
        raise ValueError("gzip hash mismatch")
    source = gzip.decompress(compressed)
    if digest(source) != EXPECTED_SOURCE_SHA256:
        raise ValueError("source hash mismatch")
    compile(source, str(output), "exec")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(source)
    print(f"source_bytes={len(source)} source_sha256={digest(source)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: restore.py OUTPUT.py")
    restore(Path(sys.argv[1]))

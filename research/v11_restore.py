from __future__ import annotations

import base64
import gzip
import hashlib
import sys
from pathlib import Path

EXPECTED_SHA256 = "cd168a660183e60538dd4729e0703ed3a34292c729a661cfe11e6abdd43d4667"


def restore(source: Path, output: Path) -> None:
    encoded = "".join(source.read_text(encoding="ascii").split())
    payload = gzip.decompress(base64.b64decode(encoded, validate=True))
    digest = hashlib.sha256(payload).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError(f"source hash mismatch: {digest} != {EXPECTED_SHA256}")
    compile(payload, str(output), "exec")
    output.write_bytes(payload)
    print(f"restored={output} bytes={len(payload)} sha256={digest}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: v11_restore.py INPUT_B64 OUTPUT_PY")
    restore(Path(sys.argv[1]), Path(sys.argv[2]))

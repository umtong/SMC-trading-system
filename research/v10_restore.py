from __future__ import annotations

import base64
import gzip
import hashlib
import sys
from pathlib import Path

EXPECTED_SHA256 = "586746eb1bfdc30e49a8f7b5fee665fafb7889b003acae4fa5cfc86f10a77f22"


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
        raise SystemExit("usage: v10_restore.py INPUT_B64 OUTPUT_PY")
    restore(Path(sys.argv[1]), Path(sys.argv[2]))

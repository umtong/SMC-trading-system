from __future__ import annotations

import base64
import hashlib
import runpy
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "run_research.py.zlib.b64"
TARGET = HERE / "run_research.py"
EXPECTED_SHA256 = "80fe8ddfdc739c03e13ddffea130a796882186d33e11d9684bddb7febc3bdfda"

raw = zlib.decompress(base64.b64decode(PAYLOAD.read_text(encoding="utf-8").strip()))
actual = hashlib.sha256(raw).hexdigest()
if actual != EXPECTED_SHA256:
    raise RuntimeError(f"research engine digest mismatch: expected {EXPECTED_SHA256}, got {actual}")
TARGET.write_bytes(raw)
runpy.run_path(str(TARGET), run_name="__main__")

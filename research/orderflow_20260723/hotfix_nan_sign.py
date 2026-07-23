from __future__ import annotations

import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "run_research.py"
SOURCE_SHA256 = "80fe8ddfdc739c03e13ddffea130a796882186d33e11d9684bddb7febc3bdfda"
PATCHED_SHA256 = "4933bdf62afabbbf3d79ef7888bc9eb7eec3f89ed4b447a211260e3851e92cdd"

raw = TARGET.read_bytes()
actual = hashlib.sha256(raw).hexdigest()
if actual == PATCHED_SHA256:
    print({"hotfix": "already_applied", "sha256": actual})
    raise SystemExit(0)
if actual != SOURCE_SHA256:
    raise RuntimeError(f"unexpected source engine digest: {actual}")

text = raw.decode("utf-8")
replacements = {
    "flow_side = np.sign(z).astype(np.int8)": "flow_side = np.sign(z.fillna(0.0)).astype(np.int8)",
    "crowd_side = np.sign(fz).astype(np.int8)": "crowd_side = np.sign(fz.fillna(0.0)).astype(np.int8)",
    "(np.sign(iz).astype(np.int8) == crowd_side)": "(np.sign(iz.fillna(0.0)).astype(np.int8) == crowd_side)",
    "trade_side = -np.sign(dz).astype(np.int8)": "trade_side = -np.sign(dz.fillna(0.0)).astype(np.int8)",
}
for old, new in replacements.items():
    count = text.count(old)
    if count == 0:
        raise RuntimeError(f"hotfix target not found: {old}")
    text = text.replace(old, new)
TARGET.write_text(text, encoding="utf-8")
patched = hashlib.sha256(TARGET.read_bytes()).hexdigest()
if patched != PATCHED_SHA256:
    raise RuntimeError(f"patched engine digest mismatch: expected {PATCHED_SHA256}, got {patched}")
print({"hotfix": "applied", "sha256": patched})

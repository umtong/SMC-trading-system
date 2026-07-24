from __future__ import annotations

import subprocess
import sys
from pathlib import Path

EXPECTED_BLOB_SHA = "ddafe1ff7e68b8e7b9baf455fd2592b019543570"

if len(sys.argv) != 2:
    raise SystemExit("usage: repair_transport_boolean_literals.py RUNNER")

path = Path(sys.argv[1])
blob = subprocess.check_output(["git", "hash-object", str(path)], text=True).strip()
if blob != EXPECTED_BLOB_SHA:
    raise RuntimeError(f"unexpected runner blob before repair: {blob} != {EXPECTED_BLOB_SHA}")

text = path.read_text(encoding="utf-8")
replacements = {
    '"test_opened": false': ('"test_opened": False', 2),
    '"orders_submitted": false': ('"orders_submitted": False', 1),
    '"paper_or_live_started": false': ('"paper_or_live_started": False', 1),
    '"production_enabled": false': ('"production_enabled": False', 1),
}
for old, (new, expected_count) in replacements.items():
    count = text.count(old)
    if count != expected_count:
        raise RuntimeError(f"ambiguous transport repair {old}: {count} != {expected_count}")
    text = text.replace(old, new)
path.write_text(text, encoding="utf-8")
print(f"repaired={path}")

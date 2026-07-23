from __future__ import annotations

import sys
from pathlib import Path


def patch(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    needle = " < free_at:"
    count = text.count(needle)
    if count != 2:
        raise ValueError(f"expected exactly two global scheduler comparisons, found {count}")
    text = text.replace(needle, " <= free_at:")
    compile(text, str(path), "exec")
    path.write_text(text, encoding="utf-8")
    print("V12 strict overlap patch applied to two schedulers")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: v12_causality_patch.py SOURCE.py")
    patch(Path(sys.argv[1]))

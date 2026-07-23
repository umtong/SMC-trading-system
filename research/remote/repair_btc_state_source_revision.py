from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: repair_btc_state_source_revision.py SCRIPT")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
old = 'REPO_REVISION = "39b19d4b296129ce5ee1e2118d2ca1c8a49c1984"'
new = 'REPO_REVISION = "728ce620e8854fde1aa2cb0bca41d8d150dca3bb"'
count = text.count(old)
if count != 1:
    raise RuntimeError(f"expected exactly one pre-raw source revision, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")

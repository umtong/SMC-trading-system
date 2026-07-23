from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: repair_cross_asset_wfv_v3_transfer.py SCRIPT")
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
repairs = {
    'params["leader_oi3z_min"]': 'params["leader_oi3_z_min"]',
    'params["follower_signed_ret3z_max"]': 'params["follower_signed_ret3_z_max"]',
}
for bad, good in repairs.items():
    count = text.count(bad)
    if count != 1:
        raise RuntimeError(f"expected exactly one transfer corruption {bad!r}; found {count}")
    text = text.replace(bad, good)
path.write_text(text, encoding="utf-8")

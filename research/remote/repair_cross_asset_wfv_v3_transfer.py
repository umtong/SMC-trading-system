from pathlib import Path
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: repair_cross_asset_wfv_v3_transfer.py SCRIPT")
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
repairs = {
    'params["leader_oi3z_min"]': 'params["leader_oi3_z_min"]',
    'params["follower_signed_ret3z_max"]': 'params["follower_signed_ret3_z_max"]',
    '''    api = HfApi()\n    info = api.dataset_info(REPO_ID)\n    revision = info.sha\n    files = api.list_repo_files(REPO_ID, repo_type="dataset", revision=revision)\n''': '''    api = HfApi()\n    revision = "0113be29cdcb7e977037d192c1055c01cf0d369e"\n    info = api.dataset_info(REPO_ID, revision=revision)\n    if info.sha != revision:\n        raise RuntimeError(f"dataset revision mismatch: {info.sha} != {revision}")\n    files = api.list_repo_files(REPO_ID, repo_type="dataset", revision=revision)\n''',
}
for bad, good in repairs.items():
    count = text.count(bad)
    if count != 1:
        raise RuntimeError(f"expected exactly one transfer/pinning replacement; found {count}: {bad[:80]!r}")
    text = text.replace(bad, good)
path.write_text(text, encoding="utf-8")

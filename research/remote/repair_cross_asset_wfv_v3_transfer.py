from pathlib import Path
import hashlib
import sys

if len(sys.argv) != 2:
    raise SystemExit("usage: repair_cross_asset_wfv_v3_transfer.py SCRIPT")

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
original_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()


def replace_once_or_confirm(source: str, target: str, label: str) -> None:
    global text
    source_count = text.count(source)
    target_count = text.count(target)
    if source_count == 1:
        text = text.replace(source, target)
        print(f"repair={label} action=replaced source_count=1 target_count_before={target_count}")
        return
    if source_count == 0 and target_count >= 1:
        print(f"repair={label} action=already_applied target_count={target_count}")
        return
    raise RuntimeError(
        f"ambiguous transfer repair {label}: source_count={source_count}, target_count={target_count}"
    )


replace_once_or_confirm(
    'params["leader_oi3az_min"]',
    'params["leader_oi3_z_min"]',
    "leader_oi_parameter",
)
replace_once_or_confirm(
    'params["follower_signed_ret3az_max"]',
    'params["follower_signed_ret3_z_max"]',
    "follower_return_parameter",
)
replace_once_or_confirm(
    '''    api = HfApi()\n    info = api.dataset_info(REPO_ID)\n    revision = info.sha\n    files = api.list_repo_files(REPO_ID, repo_type="dataset", revision=revision)\n''',
    '''    api = HfApi()\n    revision = "0113be29cdcb7e977037d192c1055c01cf0d369e"\n    info = api.dataset_info(REPO_ID, revision=revision)\n    if info.sha != revision:\n        raise RuntimeError(f"dataset revision mismatch: {info.sha} != {revision}")\n    files = api.list_repo_files(REPO_ID, repo_type="dataset", revision=revision)\n''',
    "dataset_revision_pin",
)

path.write_text(text, encoding="utf-8")
repaired_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
print(f"original_sha256={original_sha}")
print(f"repaired_sha256={repaired_sha}")

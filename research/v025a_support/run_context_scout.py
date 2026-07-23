from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.request import Request, urlopen

ARCHIVE_SHA256 = "bebc0e1277a0b7a471e8447cf667251fda9685700eb8508dacd979f15639a9b1"
DATA_PLAN_SHA256 = "9874d8042af578586bba0310e76f376873077be6c32e1d49408d3e15be5fd496"
LOCKED_PLAN_SHA256 = "080080a2ab64aff387a08584cb0b220e8f9fc9040025df32729693aa2db375bf"
SUPPORT_INIT_SHA256 = "19097e6cc0fc3b6a604adc8b759356866941f2c539304df78f48222a1617c0a1"
SUPPORT_ARCHIVE_SHA256 = "3a4e65901ab64ecbc47d65431e31fef7129a74a270a5f4d874f77234248b014a"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def prepare(repo: Path, output: Path) -> None:
    bundle = repo / "research/v025a_bundle"
    manifest = json.loads((bundle / "TRANSPORT_MANIFEST.json").read_text())
    encoded: list[bytes] = []
    evidence = []
    for item in manifest["parts"]:
        path = bundle / item["name"]
        raw = path.read_bytes()
        observed = hashlib.sha256(raw).hexdigest()
        assert len(raw) == int(item["bytes"])
        assert observed == item["sha256"]
        encoded.append(raw)
        evidence.append({"name": item["name"], "bytes": len(raw), "sha256": observed})
    assert len(encoded) == int(manifest["part_count"]) == 11
    payload = base64.b64decode(b"".join(encoded), validate=True)
    observed_archive = hashlib.sha256(payload).hexdigest()
    assert len(payload) == int(manifest["archive_bytes"])
    assert observed_archive == manifest["archive_sha256"] == ARCHIVE_SHA256
    archive = Path("/tmp/v025a_bundle.tar.xz")
    archive.write_bytes(payload)
    subprocess.run(["xz", "-t", str(archive)], check=True)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    subprocess.run(["tar", "-xJf", str(archive), "-C", str(output)], check=True)
    support = repo / "research/v025a_support/smc_ict_quant"
    package = output / "smc_ict_quant"
    package.mkdir(parents=True, exist_ok=True)
    shutil.copy2(support / "__init__.py", package / "__init__.py")
    shutil.copy2(support / "binance_public_data.py", package / "binance_public_data.py")
    checks = {
        output / "artifacts/v0243/data_acquisition_plan.json": DATA_PLAN_SHA256,
        output / "research/v025/research_plan_locked.json": LOCKED_PLAN_SHA256,
        package / "__init__.py": SUPPORT_INIT_SHA256,
        package / "binance_public_data.py": SUPPORT_ARCHIVE_SHA256,
    }
    for path, expected in checks.items():
        assert sha256(path) == expected, path
    evidence_dir = repo / "transport-evidence"
    evidence_dir.mkdir(exist_ok=True)
    (evidence_dir / "reconstruction.json").write_text(
        json.dumps({"archive_sha256": observed_archive, "archive_bytes": len(payload), "parts": evidence}, sort_keys=True, indent=2) + "\n"
    )


def scout(workspace: Path) -> dict:
    plan = json.loads((workspace / "event_day_plan.json").read_text())
    assert plan["generated_from_candidate_pnl"] is False
    assert plan["terminal_holdout_downloaded"] is False
    requests = list(plan["requests"])
    assert all(str(item["date"]) < "2025-10-01" for item in requests)

    def probe(item: dict) -> dict:
        url = str(item["url"])
        headers = {"User-Agent": "smc-ict-v025a-size-scout/1.0"}
        try:
            with urlopen(Request(url, method="HEAD", headers=headers), timeout=60) as response:
                length = response.headers.get("Content-Length")
                return {"relative_path": item["relative_path"], "bytes": int(length) if length else None, "method": "HEAD", "status": response.status}
        except Exception as head_error:
            try:
                range_headers = dict(headers)
                range_headers["Range"] = "bytes=0-0"
                with urlopen(Request(url, headers=range_headers), timeout=60) as response:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.search(r"/(\d+)$", content_range)
                    length = int(match.group(1)) if match else response.headers.get("Content-Length")
                    return {"relative_path": item["relative_path"], "bytes": int(length) if length else None, "method": "RANGE", "status": response.status, "head_error": repr(head_error)}
            except Exception as range_error:
                return {"relative_path": item["relative_path"], "bytes": None, "method": "FAILED", "error": repr(range_error), "head_error": repr(head_error)}

    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(probe, item) for item in requests]
        for index, future in enumerate(as_completed(futures), 1):
            records.append(future.result())
            if index % 50 == 0 or index == len(futures):
                print(f"probed={index}/{len(futures)}", flush=True)
    records.sort(key=lambda item: item["relative_path"])
    known = [int(item["bytes"]) for item in records if item.get("bytes") is not None]
    payload = {
        "schema_version": 1,
        "study_id": plan["study_id"],
        "event_plan_payload_sha256": plan["payload_sha256"],
        "context_count": plan["context_count"],
        "request_count": len(records),
        "known_size_count": len(known),
        "failed_size_count": len(records) - len(known),
        "total_compressed_bytes_known": sum(known),
        "max_compressed_bytes": max(known) if known else None,
        "median_compressed_bytes": sorted(known)[len(known) // 2] if known else None,
        "candidate_pnl_observed": False,
        "terminal_holdout_opened": False,
        "production_enabled": False,
        "records": records,
    }
    (workspace / "event_size_scout.json").write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    return payload


def persist(repo: Path, workspace: Path, job_status: str) -> None:
    result = repo / "research/results/v025a-context-scout"
    result.mkdir(parents=True, exist_ok=True)
    for name in ("context_summary.json", "event_size_scout.json"):
        source = workspace / name
        if source.exists():
            shutil.copy2(source, result / name)
    status = {
        "schema_version": 1,
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "head_sha": os.environ.get("GITHUB_SHA"),
        "job_status": job_status,
        "candidate_pnl_observed": False,
        "terminal_holdout_opened": False,
        "production_enabled": False,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "context_summary_present": (result / "context_summary.json").exists(),
        "event_size_scout_present": (result / "event_size_scout.json").exists(),
    }
    (result / "status.json").write_text(json.dumps(status, sort_keys=True, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--repo", type=Path, default=Path("."))
    prep.add_argument("--output", type=Path, required=True)
    size = sub.add_parser("scout")
    size.add_argument("--workspace", type=Path, required=True)
    save = sub.add_parser("persist")
    save.add_argument("--repo", type=Path, default=Path("."))
    save.add_argument("--workspace", type=Path, required=True)
    save.add_argument("--job-status", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.repo.resolve(), args.output.resolve())
    elif args.command == "scout":
        summary = scout(args.workspace.resolve())
        print(json.dumps({k: v for k, v in summary.items() if k != "records"}, sort_keys=True, indent=2))
    else:
        persist(args.repo.resolve(), args.workspace.resolve(), args.job_status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

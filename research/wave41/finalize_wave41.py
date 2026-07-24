from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.wave39.wave39_engine import sha256_file, stable_candidate_id


IDENTITY_KEYS = (
    "family", "score_quantile", "volume_quantile", "flow_price_weight",
    "trend_mode", "clock_mode", "horizon_minutes", "stop_atr",
    "base_latency_minutes",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--freeze-dir", type=Path, required=True)
    args = parser.parse_args()
    args.freeze_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.result_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate_path = args.result_dir / "wave41_selected_candidate_2022.json"
    present = bool(manifest.get("development_gate_count", 0) > 0 and candidate_path.exists())
    if not present:
        blocked = {
            "schema": "wave41-development-block-v1",
            "registration_sha256": sha256_file(args.registration),
            "development_manifest_sha256": sha256_file(manifest_path),
            "candidate_count": manifest["candidate_count"],
            "development_gate_count": manifest["development_gate_count"],
            "2023_opened": False,
            "2024_opened": False,
            "sealed_terminal_oos_opened": False,
            "risk_or_leverage_optimized": False,
            "next_action": "block the registered cross-sectional residual-flow grid and rotate to an independent mechanism",
        }
        (args.result_dir / "BLOCKED.json").write_text(
            json.dumps(blocked, indent=2, sort_keys=True), encoding="utf-8"
        )
        print(json.dumps({"candidate_present": False}, sort_keys=True))
        return 0

    selected = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate = selected["candidate"]
    identity = {key: candidate[key] for key in IDENTITY_KEYS}
    if stable_candidate_id(identity) != candidate["candidate_id"]:
        raise RuntimeError("Wave41 candidate identity mismatch")
    paths = [
        args.registration,
        Path("research/wave39/wave39_engine.py"),
        Path("research/wave39/wave39_engine_v3.py"),
        Path("research/wave39/wave39_engine_v4.py"),
        Path("research/wave41/run_wave41_development.py"),
        Path("research/wave41/run_wave41_development_v2.py"),
        manifest_path,
        args.result_dir / "wave41_all_candidates_2022.csv.gz",
        args.result_dir / "wave41_gated_candidates_2022.csv",
        candidate_path,
        args.result_dir / "wave41_selected_ledger_2022.csv",
        args.data_root / "manifest.json",
        args.data_root / "support" / "manifest.json",
    ]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise RuntimeError(f"freeze inputs missing: {missing}")
    freeze = {
        "schema": "wave41-candidate-freeze-before-2023-v1",
        "candidate": candidate,
        "selected_audit": selected["audit"],
        "frozen_files": {
            str(path): {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for path in paths
        },
        "2023_outcome_opened_before_freeze": False,
        "2024_opened": False,
        "sealed_terminal_oos_opened": False,
        "risk_or_leverage_optimized": False,
        "missing_data_policy": "No imputation; no open path can cross an absent official minute.",
    }
    output = args.freeze_dir / "WAVE41_CANDIDATE_BEFORE_2023.json"
    output.write_text(json.dumps(freeze, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "candidate_present": True,
        "candidate_id": candidate["candidate_id"],
        "freeze_sha256": sha256_file(output),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

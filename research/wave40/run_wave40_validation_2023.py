from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research.wave39 import run_wave39_development as base
from research.wave39.wave39_engine import greedy_one_slot, metrics, sha256_file, stable_candidate_id
from research.wave39.wave39_engine_v4 import prior_atr_and_trends, simulate_stop_time_paths
from research.wave40 import run_wave40_development as wave40


base.prior_atr_and_trends = prior_atr_and_trends
base.simulate_stop_time_paths = simulate_stop_time_paths
wave40.base.prior_atr_and_trends = prior_atr_and_trends
wave40.base.simulate_stop_time_paths = simulate_stop_time_paths

START_MS = int(pd.Timestamp("2023-01-01T00:00:00Z").timestamp() * 1000)
END_MS = int(pd.Timestamp("2024-01-01T00:00:00Z").timestamp() * 1000)
QUARTERS = tuple(
    (int(pd.Timestamp(start).timestamp() * 1000), int(pd.Timestamp(end).timestamp() * 1000))
    for start, end in (
        ("2023-01-01T00:00:00Z", "2023-04-01T00:00:00Z"),
        ("2023-04-01T00:00:00Z", "2023-07-01T00:00:00Z"),
        ("2023-07-01T00:00:00Z", "2023-10-01T00:00:00Z"),
        ("2023-10-01T00:00:00Z", "2024-01-01T00:00:00Z"),
    )
)
IDENTITY_KEYS = (
    "symbol", "family", "post_quantile", "volume_quantile", "pre_quantile",
    "top_quantile", "count_quantile", "disagreement_quantile", "trend_mode",
    "clock_mode", "horizon_minutes", "stop_atr", "base_latency_minutes",
)


def normalize(value):
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-2022", type=Path, required=True)
    parser.add_argument("--data-2023", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def concat_boundary(root_2022: Path, root_2023: Path, symbol: str) -> pd.DataFrame:
    frames = [
        pd.read_csv(root / f"{symbol}_quarterhour_exact_{year}.csv.gz")
        for year, root in ((2022, root_2022), (2023, root_2023))
    ]
    frame = pd.concat(frames, ignore_index=True)
    clock = frame["boundary_time_ms"].to_numpy(np.int64)
    if np.any(np.diff(clock) != 900_000):
        raise RuntimeError("boundary clock discontinuity")
    return frame


def concat_support(root_2022: Path, root_2023: Path, symbol: str):
    contract = pd.concat([
        pd.read_csv(root_2022 / "support" / f"{symbol}_contract_1m_2022.csv.gz"),
        pd.read_csv(root_2023 / "support" / f"{symbol}_contract_1m_2023.csv.gz"),
    ], ignore_index=True)
    clock = contract["open_time_ms"].to_numpy(np.int64)
    if np.any(np.diff(clock) != 60_000):
        raise RuntimeError("support clock discontinuity")
    funding = pd.concat([
        pd.read_csv(root_2022 / "support" / f"{symbol}_funding_2022.csv.gz"),
        pd.read_csv(root_2023 / "support" / f"{symbol}_funding_2023.csv.gz"),
    ], ignore_index=True)
    funding.sort_values("funding_time_ms", inplace=True, kind="mergesort")
    funding.drop_duplicates("funding_time_ms", keep="last", inplace=True)
    funding.reset_index(drop=True, inplace=True)
    return contract, funding


def gate(costs, latency2, opposite):
    m24 = costs["24"]; m32 = costs["32"]
    checks = {
        "minimum_completed_trades": int(m24["trades"]) >= 60,
        "positive_net_log_growth_24bp": float(m24["net_log_growth"]) > 0.0,
        "positive_net_log_growth_32bp": float(m32["net_log_growth"]) > 0.0,
        "positive_quarters_24bp": int(m24["positive_folds"]) >= 3,
        "positive_months_24bp": int(m24["positive_months"]) >= 7,
        "profit_factor_32bp": float(m32["profit_factor"]) >= 1.10,
        "net_after_top5_24bp": float(m24["net_after_top5"]) > 0.0,
        "latency2_net_growth_24bp": float(latency2["net_log_growth"]) > 0.0,
        "opposite_direction_control_negative_24bp": float(opposite["net_log_growth"]) < 0.0,
    }
    return bool(all(checks.values())), checks


def main() -> int:
    arguments = args(); arguments.output_dir.mkdir(parents=True, exist_ok=True)
    freeze = json.loads(arguments.freeze.read_text(encoding="utf-8"))
    if freeze.get("schema") != "wave40-candidate-freeze-before-2023-v1":
        raise RuntimeError("freeze schema mismatch")
    if freeze.get("2023_outcome_opened_before_freeze") is not False:
        raise RuntimeError("freeze chronology violated")
    candidate = {key: normalize(value) for key, value in freeze["candidate"].items()}
    identity = {key: candidate[key] for key in IDENTITY_KEYS}
    if stable_candidate_id(identity) != candidate["candidate_id"]:
        raise RuntimeError("candidate ID mismatch")
    symbol = str(candidate["symbol"])
    boundary = concat_boundary(arguments.data_2022, arguments.data_2023, symbol)
    contract, funding = concat_support(arguments.data_2022, arguments.data_2023, symbol)
    paths, trends = base.build_paths(symbol, boundary, contract, funding)
    value = wave40.components(boundary)
    threshold = wave40.thresholds(value)
    params = {key: candidate[key] for key in (
        "post_quantile", "volume_quantile", "pre_quantile", "top_quantile",
        "count_quantile", "disagreement_quantile",
    )}
    mask, side, _ = wave40.family_mask_side(
        str(candidate["family"]), params, value, threshold
    )
    event_times = boundary["boundary_time_ms"].to_numpy(np.int64)
    mask &= base.trend_mask(trends, str(candidate["trend_mode"]), side)
    mask &= base.clock_mask(event_times, str(candidate["clock_mode"]))
    mask &= (event_times >= START_MS) & (event_times < END_MS)
    hi = int(np.where(wave40.HORIZONS == int(candidate["horizon_minutes"]))[0][0])
    si = int(np.where(wave40.STOPS == float(candidate["stop_atr"]))[0][0])

    cost_metrics = {}; arrays = {}; base_selected = None; base_entry = None; base_exit = None
    for cost in (18.0, 24.0, 32.0, 40.0):
        net, entry, exit_time, stopped = paths.outcome(
            side=side, horizon_index=hi, stop_index=si, latency_index=0,
            round_trip_bp=cost,
        )
        selected = greedy_one_slot(np.flatnonzero(mask & np.isfinite(net)), entry, exit_time)
        cost_metrics[str(int(cost))] = metrics(net[selected], entry[selected], fold_edges_ms=QUARTERS)
        arrays[int(cost)] = (net, entry, exit_time, stopped, selected)
        if cost == 24.0:
            base_selected, base_entry, base_exit = selected, entry, exit_time
    assert base_selected is not None and base_entry is not None and base_exit is not None

    latency2_net, latency2_entry, latency2_exit, _ = paths.outcome(
        side=side, horizon_index=hi, stop_index=si, latency_index=1, round_trip_bp=24.0
    )
    latency2_selected = greedy_one_slot(
        np.flatnonzero(mask & np.isfinite(latency2_net)), latency2_entry, latency2_exit
    )
    latency2_metrics = metrics(
        latency2_net[latency2_selected], latency2_entry[latency2_selected], fold_edges_ms=QUARTERS
    )
    opposite_net, opposite_entry, opposite_exit, _ = paths.outcome(
        side=-side, horizon_index=hi, stop_index=si, latency_index=0, round_trip_bp=24.0
    )
    opposite_selected = greedy_one_slot(
        np.flatnonzero(mask & np.isfinite(opposite_net)), opposite_entry, opposite_exit
    )
    opposite_metrics = metrics(
        opposite_net[opposite_selected], opposite_entry[opposite_selected], fold_edges_ms=QUARTERS
    )
    passed, checks = gate(cost_metrics, latency2_metrics, opposite_metrics)

    ledger_rows = []
    for index in base_selected:
        row = {
            "event_index_combined": int(index), "event_time_ms": int(event_times[index]),
            "entry_time_ms": int(base_entry[index]), "exit_time_ms": int(base_exit[index]),
            "side": int(side[index]), "symbol": symbol,
        }
        for cost, values in arrays.items():
            row[f"net_log_{cost}bp"] = float(values[0][index])
            row[f"stopped_{cost}bp"] = int(values[3][index])
        ledger_rows.append(row)
    ledger_path = arguments.output_dir / "wave40_validation_2023_ledger.csv"
    pd.DataFrame(ledger_rows).to_csv(ledger_path, index=False)

    input_hashes = {}
    for year, root in ((2022, arguments.data_2022), (2023, arguments.data_2023)):
        for relative in (
            Path(f"{symbol}_quarterhour_exact_{year}.csv.gz"),
            Path("support") / f"{symbol}_contract_1m_{year}.csv.gz",
            Path("support") / f"{symbol}_funding_{year}.csv.gz",
        ):
            input_hashes[f"{year}/{relative}"] = sha256_file(root / relative)
    result = {
        "schema": "wave40-frozen-validation-2023-v1",
        "candidate_id": candidate["candidate_id"], "candidate": candidate,
        "freeze_sha256": sha256_file(arguments.freeze), "input_hashes": input_hashes,
        "cost_metrics": cost_metrics, "latency2_24bp": latency2_metrics,
        "opposite_direction_24bp": opposite_metrics, "gate_checks": checks,
        "frozen_2023_gate_passed": passed,
        "2024_opened": False, "sealed_terminal_oos_opened": False,
        "risk_or_leverage_optimized": False,
        "ledger": {"path": ledger_path.name, "bytes": ledger_path.stat().st_size, "sha256": sha256_file(ledger_path)},
    }
    result_path = arguments.output_dir / "wave40_validation_2023.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    manifest = {
        "schema": "wave40-validation-manifest-v1",
        "candidate_id": candidate["candidate_id"], "gate_passed": passed,
        "result_sha256": sha256_file(result_path), "ledger_sha256": sha256_file(ledger_path),
        "next_action": (
            "freeze unchanged for a separately registered 2024 walk-forward"
            if passed else "block Wave40 and rotate to an independent mechanism"
        ),
    }
    (arguments.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

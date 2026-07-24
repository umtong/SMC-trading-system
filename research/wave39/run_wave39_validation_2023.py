from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.wave39 import run_wave39_development as base
from research.wave39 import run_wave39_development_v2 as _flow_flip_patch  # noqa: F401
from research.wave39.wave39_engine import (
    greedy_one_slot,
    metrics,
    sha256_file,
    stable_candidate_id,
)


VALIDATION_START = pd.Timestamp("2023-01-01T00:00:00Z")
VALIDATION_END = pd.Timestamp("2024-01-01T00:00:00Z")
VALIDATION_START_MS = int(VALIDATION_START.timestamp() * 1000)
VALIDATION_END_MS = int(VALIDATION_END.timestamp() * 1000)
QUARTER_EDGES = tuple(
    (
        int(pd.Timestamp(start).timestamp() * 1000),
        int(pd.Timestamp(end).timestamp() * 1000),
    )
    for start, end in (
        ("2023-01-01T00:00:00Z", "2023-04-01T00:00:00Z"),
        ("2023-04-01T00:00:00Z", "2023-07-01T00:00:00Z"),
        ("2023-07-01T00:00:00Z", "2023-10-01T00:00:00Z"),
        ("2023-10-01T00:00:00Z", "2024-01-01T00:00:00Z"),
    )
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-2022", type=Path, required=True)
    parser.add_argument("--data-2023", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def concatenate_boundary(root_2022: Path, root_2023: Path, symbol: str) -> pd.DataFrame:
    frames = []
    for year, root in ((2022, root_2022), (2023, root_2023)):
        path = root / f"{symbol}_quarterhour_exact_{year}.csv.gz"
        frame = pd.read_csv(path)
        expected = (365 * 96)
        if len(frame) != expected:
            raise RuntimeError(f"{symbol}/{year}: boundary rows {len(frame)} != {expected}")
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    times = combined["boundary_time_ms"].to_numpy(dtype=np.int64)
    if np.any(np.diff(times) != 900_000):
        raise RuntimeError(f"{symbol}: boundary continuity failure across years")
    return combined


def concatenate_support(
    root_2022: Path,
    root_2023: Path,
    symbol: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    contract_frames = []
    funding_frames = []
    for year, root in ((2022, root_2022), (2023, root_2023)):
        support = root / "support"
        contract_frames.append(pd.read_csv(support / f"{symbol}_contract_1m_{year}.csv.gz"))
        funding_frames.append(pd.read_csv(support / f"{symbol}_funding_{year}.csv.gz"))
    contract = pd.concat(contract_frames, ignore_index=True)
    minute_time = contract["open_time_ms"].to_numpy(dtype=np.int64)
    if np.any(np.diff(minute_time) != 60_000):
        raise RuntimeError(f"{symbol}: support minute continuity failure across years")
    funding = pd.concat(funding_frames, ignore_index=True)
    funding.sort_values("funding_time_ms", kind="mergesort", inplace=True)
    funding.drop_duplicates("funding_time_ms", keep="last", inplace=True)
    funding.reset_index(drop=True, inplace=True)
    return contract, funding


def build_signal(
    candidate: dict[str, Any],
    boundary: pd.DataFrame,
    components,
    thresholds,
    trade_trends,
) -> tuple[np.ndarray, np.ndarray]:
    family = str(candidate["family"])
    window = int(candidate["window_seconds"])
    mask, side = base.candidate_mask_and_side(
        family=family,
        window=window,
        components=components,
        thresholds=thresholds,
        q_imbalance=float(candidate["imbalance_quantile"]),
        q_volume=float(candidate["volume_quantile"]),
    )
    mask &= base.trend_mask(trade_trends, str(candidate["trend_mode"]), side)
    event_times = boundary["boundary_time_ms"].to_numpy(dtype=np.int64)
    mask &= base.clock_mask(event_times, str(candidate["clock_mode"]))
    mask &= (event_times >= VALIDATION_START_MS) & (event_times < VALIDATION_END_MS)
    return mask, side


def gate(metrics_by_cost: dict[str, dict[str, Any]], latency2: dict[str, Any], opposite: dict[str, Any]) -> tuple[bool, dict[str, bool]]:
    m24 = metrics_by_cost["24"]
    m32 = metrics_by_cost["32"]
    checks = {
        "minimum_completed_trades": int(m24["trades"]) >= 80,
        "positive_net_log_growth_24bp": float(m24["net_log_growth"]) > 0.0,
        "positive_net_log_growth_32bp": float(m32["net_log_growth"]) > 0.0,
        "positive_quarter_count_24bp": int(m24["positive_folds"]) >= 3,
        "positive_month_count_24bp": int(m24["positive_months"]) >= 8,
        "profit_factor_32bp": float(m32["profit_factor"]) >= 1.10,
        "top10_winner_share_24bp": float(m24["top10_winner_share"]) <= 0.50,
        "net_after_top5_24bp": float(m24["net_after_top5"]) > 0.0,
        "latency2_net_growth_24bp": float(latency2["net_log_growth"]) > 0.0,
        "opposite_direction_control_negative_24bp": float(opposite["net_log_growth"]) < 0.0,
    }
    return bool(all(checks.values())), checks


def main() -> int:
    args = arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    if freeze.get("schema") != "wave39-candidate-freeze-before-2023-v1":
        raise RuntimeError("freeze schema mismatch")
    if freeze.get("2023_outcome_opened_before_freeze") is not False:
        raise RuntimeError("freeze chronology violated")
    candidate = dict(freeze["candidate"])
    identity_fields = {
        key: candidate[key]
        for key in (
            "source_symbol", "trade_symbol", "family", "window_seconds",
            "imbalance_quantile", "volume_quantile", "trend_mode", "clock_mode",
            "horizon_minutes", "stop_atr", "base_latency_minutes",
        )
    }
    if stable_candidate_id(identity_fields) != candidate["candidate_id"]:
        raise RuntimeError("frozen candidate ID mismatch")

    source_symbol = str(candidate["source_symbol"])
    trade_symbol = str(candidate["trade_symbol"])
    boundaries: dict[str, pd.DataFrame] = {}
    components = {}
    thresholds = {}
    paths = {}
    trends = {}
    required_symbols = tuple(dict.fromkeys((source_symbol, trade_symbol)))
    input_hashes: dict[str, str] = {}

    for symbol in required_symbols:
        boundary = concatenate_boundary(args.data_2022, args.data_2023, symbol)
        contract, funding = concatenate_support(args.data_2022, args.data_2023, symbol)
        path, symbol_trends = base.build_paths(symbol, boundary, contract, funding)
        boundaries[symbol] = boundary
        components[symbol] = base.signal_components(boundary)
        thresholds[symbol] = base.build_thresholds(components[symbol])
        paths[symbol] = path
        trends[symbol] = symbol_trends
        for year, root in ((2022, args.data_2022), (2023, args.data_2023)):
            for relative in (
                Path(f"{symbol}_quarterhour_exact_{year}.csv.gz"),
                Path("support") / f"{symbol}_contract_1m_{year}.csv.gz",
                Path("support") / f"{symbol}_funding_{year}.csv.gz",
            ):
                path_value = root / relative
                input_hashes[f"{year}/{relative}"] = sha256_file(path_value)

    source_boundary = boundaries[source_symbol]
    mask, side = build_signal(
        candidate,
        source_boundary,
        components[source_symbol],
        thresholds[source_symbol],
        trends[trade_symbol],
    )
    path = paths[trade_symbol]
    horizon_index = int(np.where(base.HORIZONS == int(candidate["horizon_minutes"]))[0][0])
    stop_index = int(np.where(base.STOPS == float(candidate["stop_atr"]))[0][0])

    cost_results = {}
    ledger_columns: dict[str, np.ndarray] = {}
    base_selected = None
    base_entry = None
    base_exit = None
    for cost in (18.0, 24.0, 32.0, 40.0):
        net, entry, exit_time, stopped = path.outcome(
            side=side,
            horizon_index=horizon_index,
            stop_index=stop_index,
            latency_index=0,
            round_trip_bp=cost,
        )
        selected = greedy_one_slot(np.flatnonzero(mask & np.isfinite(net)), entry, exit_time)
        cost_results[str(int(cost))] = metrics(
            net[selected], entry[selected], fold_edges_ms=QUARTER_EDGES
        )
        ledger_columns[f"net_log_{int(cost)}bp"] = net
        ledger_columns[f"stopped_{int(cost)}bp"] = stopped
        if cost == 24.0:
            base_selected = selected
            base_entry = entry
            base_exit = exit_time
    assert base_selected is not None and base_entry is not None and base_exit is not None

    latency2_net, latency2_entry, latency2_exit, _ = path.outcome(
        side=side,
        horizon_index=horizon_index,
        stop_index=stop_index,
        latency_index=1,
        round_trip_bp=24.0,
    )
    latency2_selected = greedy_one_slot(
        np.flatnonzero(mask & np.isfinite(latency2_net)), latency2_entry, latency2_exit
    )
    latency2_metrics = metrics(
        latency2_net[latency2_selected],
        latency2_entry[latency2_selected],
        fold_edges_ms=QUARTER_EDGES,
    )

    opposite_net, opposite_entry, opposite_exit, _ = path.outcome(
        side=-side,
        horizon_index=horizon_index,
        stop_index=stop_index,
        latency_index=0,
        round_trip_bp=24.0,
    )
    opposite_selected = greedy_one_slot(
        np.flatnonzero(mask & np.isfinite(opposite_net)), opposite_entry, opposite_exit
    )
    opposite_metrics = metrics(
        opposite_net[opposite_selected],
        opposite_entry[opposite_selected],
        fold_edges_ms=QUARTER_EDGES,
    )
    passed, checks = gate(cost_results, latency2_metrics, opposite_metrics)

    event_times = source_boundary["boundary_time_ms"].to_numpy(dtype=np.int64)
    ledger_rows = []
    for index in base_selected:
        row = {
            "event_index_combined": int(index),
            "event_time_ms": int(event_times[index]),
            "entry_time_ms": int(base_entry[index]),
            "exit_time_ms": int(base_exit[index]),
            "side": int(side[index]),
            "source_symbol": source_symbol,
            "trade_symbol": trade_symbol,
        }
        for key, values in ledger_columns.items():
            row[key] = float(values[index]) if key.startswith("net_log") else int(values[index])
        ledger_rows.append(row)
    ledger = pd.DataFrame(ledger_rows)
    ledger_path = args.output_dir / "wave39_validation_2023_ledger.csv"
    ledger.to_csv(ledger_path, index=False)

    result = {
        "schema": "wave39-frozen-validation-2023-v1",
        "candidate_id": candidate["candidate_id"],
        "candidate": candidate,
        "freeze_sha256": sha256_file(args.freeze),
        "input_hashes": input_hashes,
        "cost_metrics": cost_results,
        "latency2_24bp": latency2_metrics,
        "opposite_direction_24bp": opposite_metrics,
        "gate_checks": checks,
        "frozen_2023_gate_passed": passed,
        "2024_opened": False,
        "sealed_terminal_oos_opened": False,
        "risk_or_leverage_optimized": False,
        "ledger": {
            "path": ledger_path.name,
            "bytes": ledger_path.stat().st_size,
            "sha256": sha256_file(ledger_path),
        },
    }
    result_path = args.output_dir / "wave39_validation_2023.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    manifest = {
        "schema": "wave39-validation-manifest-v1",
        "candidate_id": candidate["candidate_id"],
        "gate_passed": passed,
        "result_sha256": sha256_file(result_path),
        "ledger_sha256": sha256_file(ledger_path),
        "next_action": (
            "freeze Wave39 unchanged for a separately registered 2024 walk-forward"
            if passed
            else "block Wave39 exact-boundary family and rotate economic mechanism"
        ),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

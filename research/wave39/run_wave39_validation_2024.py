from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.director.multiyear import (
    authorize_2024_candidate,
    common_2024_gate,
    concatenate_boundary,
    concatenate_support,
    input_hashes,
    quarter_edges_ms,
    year_edges_ms,
)
from research.wave39 import run_wave39_development as base
from research.wave39 import run_wave39_development_v2 as _flow_flip_patch  # noqa: F401
from research.wave39.wave39_engine import greedy_one_slot, metrics, sha256_file, stable_candidate_id
from research.wave39.wave39_engine_v4 import prior_atr_and_trends, simulate_stop_time_paths


base.prior_atr_and_trends = prior_atr_and_trends
base.simulate_stop_time_paths = simulate_stop_time_paths

IDENTITY_KEYS = (
    "source_symbol", "trade_symbol", "family", "window_seconds",
    "imbalance_quantile", "volume_quantile", "trend_mode", "clock_mode",
    "horizon_minutes", "stop_atr", "base_latency_minutes",
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-2022", type=Path, required=True)
    parser.add_argument("--data-2023", type=Path, required=True)
    parser.add_argument("--data-2024", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--result-2023", type=Path, required=True)
    parser.add_argument("--director-registration", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    if freeze.get("schema") != "wave39-candidate-freeze-before-2023-v1":
        raise RuntimeError("Wave39 freeze schema mismatch")
    candidate = freeze["candidate"]
    identity = {key: candidate[key] for key in IDENTITY_KEYS}
    if stable_candidate_id(identity) != candidate["candidate_id"]:
        raise RuntimeError("Wave39 candidate identity mismatch")
    authorize_2024_candidate(
        director_registration=args.director_registration,
        wave=39,
        candidate_id=candidate["candidate_id"],
        freeze_path=args.freeze,
        result_2023_path=args.result_2023,
    )

    roots = {2022: args.data_2022, 2023: args.data_2023, 2024: args.data_2024}
    source_symbol = str(candidate["source_symbol"])
    trade_symbol = str(candidate["trade_symbol"])
    required_symbols = tuple(dict.fromkeys((source_symbol, trade_symbol)))
    boundaries = {}
    components = {}
    thresholds = {}
    paths = {}
    trends = {}
    for symbol in required_symbols:
        boundary = concatenate_boundary(roots, symbol)
        contract, funding = concatenate_support(roots, symbol)
        path, symbol_trends = base.build_paths(symbol, boundary, contract, funding)
        boundaries[symbol] = boundary
        components[symbol] = base.signal_components(boundary)
        thresholds[symbol] = base.build_thresholds(components[symbol])
        paths[symbol] = path
        trends[symbol] = symbol_trends

    source_boundary = boundaries[source_symbol]
    family = str(candidate["family"])
    window = int(candidate["window_seconds"])
    mask, side = base.candidate_mask_and_side(
        family=family,
        window=window,
        components=components[source_symbol],
        thresholds=thresholds[source_symbol],
        q_imbalance=float(candidate["imbalance_quantile"]),
        q_volume=float(candidate["volume_quantile"]),
    )
    mask &= base.trend_mask(trends[trade_symbol], str(candidate["trend_mode"]), side)
    event_times = source_boundary["boundary_time_ms"].to_numpy(dtype=np.int64)
    mask &= base.clock_mask(event_times, str(candidate["clock_mode"]))
    start_ms, end_ms = year_edges_ms(2024)
    mask &= (event_times >= start_ms) & (event_times < end_ms)

    path = paths[trade_symbol]
    horizon_index = int(np.where(base.HORIZONS == int(candidate["horizon_minutes"]))[0][0])
    stop_index = int(np.where(base.STOPS == float(candidate["stop_atr"]))[0][0])
    quarters = quarter_edges_ms(2024)
    arrays = {}
    cost_metrics = {}
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
        cost_metrics[str(int(cost))] = metrics(net[selected], entry[selected], fold_edges_ms=quarters)
        arrays[int(cost)] = (net, entry, exit_time, stopped, selected)
        if cost == 24.0:
            base_selected, base_entry, base_exit = selected, entry, exit_time
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
        latency2_net[latency2_selected], latency2_entry[latency2_selected], fold_edges_ms=quarters
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
        opposite_net[opposite_selected], opposite_entry[opposite_selected], fold_edges_ms=quarters
    )
    passed, checks = common_2024_gate(cost_metrics, latency2_metrics, opposite_metrics)

    ledger_rows = []
    for index in base_selected:
        row = {
            "event_index_combined": int(index),
            "event_time_ms": int(event_times[index]),
            "entry_time_ms": int(base_entry[index]),
            "exit_time_ms": int(base_exit[index]),
            "source_symbol": source_symbol,
            "trade_symbol": trade_symbol,
            "side": int(side[index]),
        }
        for cost, values in arrays.items():
            row[f"net_log_{cost}bp"] = float(values[0][index])
            row[f"stopped_{cost}bp"] = int(values[3][index])
        ledger_rows.append(row)
    ledger_path = args.output_dir / "wave39_validation_2024_ledger.csv"
    pd.DataFrame(ledger_rows).to_csv(ledger_path, index=False)

    result = {
        "schema": "wave39-authorized-validation-2024-v1",
        "candidate_id": candidate["candidate_id"],
        "candidate": candidate,
        "director_registration_sha256": sha256_file(args.director_registration),
        "freeze_sha256": sha256_file(args.freeze),
        "result_2023_sha256": sha256_file(args.result_2023),
        "input_hashes": input_hashes(roots, required_symbols),
        "cost_metrics": cost_metrics,
        "latency2_24bp": latency2_metrics,
        "opposite_direction_24bp": opposite_metrics,
        "gate_checks": checks,
        "walkforward_2024_gate_passed": passed,
        "sealed_terminal_oos_opened": False,
        "risk_or_leverage_optimized": False,
        "ledger": {
            "path": ledger_path.name,
            "bytes": ledger_path.stat().st_size,
            "sha256": sha256_file(ledger_path),
        },
    }
    result_path = args.output_dir / "wave39_validation_2024.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    manifest = {
        "schema": "wave39-validation-2024-manifest-v1",
        "candidate_id": candidate["candidate_id"],
        "gate_passed": passed,
        "result_sha256": sha256_file(result_path),
        "ledger_sha256": sha256_file(ledger_path),
        "next_action": (
            "retain unchanged for portfolio construction and sealed terminal OOS preregistration"
            if passed
            else "block Wave39 after 2024 and rotate mechanism"
        ),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
from research.wave39.wave39_engine import greedy_one_slot, metrics, sha256_file, stable_candidate_id
from research.wave39.wave39_engine_v4 import prior_atr_and_trends, simulate_stop_time_paths
from research.wave41 import run_wave41_development as dev
from research.wave41 import run_wave41_development_v2 as dev_v2


base.prior_atr_and_trends = prior_atr_and_trends
base.simulate_stop_time_paths = simulate_stop_time_paths
dev.base.prior_atr_and_trends = prior_atr_and_trends
dev.base.simulate_stop_time_paths = simulate_stop_time_paths
dev.rolling_beta = dev_v2.rolling_beta_v2

IDENTITY_KEYS = (
    "family", "score_quantile", "volume_quantile", "flow_price_weight",
    "trend_mode", "clock_mode", "horizon_minutes", "stop_atr",
    "base_latency_minutes",
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
    if freeze.get("schema") != "wave41-candidate-freeze-before-2023-v1":
        raise RuntimeError("Wave41 freeze schema mismatch")
    candidate = freeze["candidate"]
    identity = {key: candidate[key] for key in IDENTITY_KEYS}
    if stable_candidate_id(identity) != candidate["candidate_id"]:
        raise RuntimeError("Wave41 candidate identity mismatch")
    authorize_2024_candidate(
        director_registration=args.director_registration,
        wave=41,
        candidate_id=candidate["candidate_id"],
        freeze_path=args.freeze,
        result_2023_path=args.result_2023,
    )

    roots = {2022: args.data_2022, 2023: args.data_2023, 2024: args.data_2024}
    boundaries = {}
    paths = {}
    trends = {}
    for symbol in dev.SYMBOLS:
        boundary = concatenate_boundary(roots, symbol)
        contract, funding = concatenate_support(roots, symbol)
        symbol_paths, symbol_trends = base.build_paths(symbol, boundary, contract, funding)
        boundaries[symbol] = boundary
        paths[symbol] = symbol_paths
        trends[symbol] = symbol_trends

    state = dev.build_cross_section(boundaries)
    event_times = state["clock"]
    selected_asset, side, score = dev.choose_family(
        str(candidate["family"]), float(candidate["flow_price_weight"]), state
    )
    score_threshold = dev.prior_quantile(score, float(candidate["score_quantile"]))
    chosen_volume = dev.selected_vector(state["log_volume"], selected_asset)
    chosen_volume_threshold = dev.selected_vector(
        state["volume_thresholds"][float(candidate["volume_quantile"])], selected_asset
    )
    start_ms, end_ms = year_edges_ms(2024)
    mask = (
        (selected_asset >= 0)
        & np.isfinite(score)
        & (score >= score_threshold)
        & np.isfinite(chosen_volume)
        & (chosen_volume >= chosen_volume_threshold)
        & dev.selected_trend_mask(
            trends, str(candidate["trend_mode"]), selected_asset, side
        )
        & base.clock_mask(event_times, str(candidate["clock_mode"]))
        & (event_times >= start_ms)
        & (event_times < end_ms)
    )
    horizon_index = int(np.where(dev.HORIZONS == int(candidate["horizon_minutes"]))[0][0])
    stop_index = int(np.where(dev.STOPS == float(candidate["stop_atr"]))[0][0])
    quarters = quarter_edges_ms(2024)

    arrays = {}
    cost_metrics = {}
    base_selected = None
    base_entry = None
    base_exit = None
    for cost in (18.0, 24.0, 32.0, 40.0):
        net, entry, exit_time, stopped = dev.gather_outcome(
            paths, selected_asset, side, horizon_index, stop_index, 0, cost
        )
        selected = greedy_one_slot(np.flatnonzero(mask & np.isfinite(net)), entry, exit_time)
        cost_metrics[str(int(cost))] = metrics(net[selected], entry[selected], fold_edges_ms=quarters)
        arrays[int(cost)] = (net, entry, exit_time, stopped, selected)
        if cost == 24.0:
            base_selected, base_entry, base_exit = selected, entry, exit_time
    assert base_selected is not None and base_entry is not None and base_exit is not None

    latency2_net, latency2_entry, latency2_exit, _ = dev.gather_outcome(
        paths, selected_asset, side, horizon_index, stop_index, 1, 24.0
    )
    latency2_selected = greedy_one_slot(
        np.flatnonzero(mask & np.isfinite(latency2_net)), latency2_entry, latency2_exit
    )
    latency2_metrics = metrics(
        latency2_net[latency2_selected], latency2_entry[latency2_selected], fold_edges_ms=quarters
    )
    opposite_net, opposite_entry, opposite_exit, _ = dev.gather_outcome(
        paths, selected_asset, -side, horizon_index, stop_index, 0, 24.0
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
            "symbol": dev.SYMBOLS[int(selected_asset[index])],
            "side": int(side[index]),
            "score": float(score[index]),
        }
        for cost, values in arrays.items():
            row[f"net_log_{cost}bp"] = float(values[0][index])
            row[f"stopped_{cost}bp"] = int(values[3][index])
        ledger_rows.append(row)
    ledger_path = args.output_dir / "wave41_validation_2024_ledger.csv"
    pd.DataFrame(ledger_rows).to_csv(ledger_path, index=False)

    result = {
        "schema": "wave41-authorized-validation-2024-v1",
        "candidate_id": candidate["candidate_id"],
        "candidate": candidate,
        "director_registration_sha256": sha256_file(args.director_registration),
        "freeze_sha256": sha256_file(args.freeze),
        "result_2023_sha256": sha256_file(args.result_2023),
        "input_hashes": input_hashes(roots, dev.SYMBOLS),
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
    result_path = args.output_dir / "wave41_validation_2024.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    manifest = {
        "schema": "wave41-validation-2024-manifest-v1",
        "candidate_id": candidate["candidate_id"],
        "gate_passed": passed,
        "result_sha256": sha256_file(result_path),
        "ledger_sha256": sha256_file(ledger_path),
        "next_action": (
            "retain unchanged for portfolio construction and sealed terminal OOS preregistration"
            if passed
            else "block Wave41 after 2024 and rotate mechanism"
        ),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

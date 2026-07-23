from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.wave39 import run_wave39_development as base
from research.wave39.wave39_engine import greedy_one_slot, metrics, sha256_file, stable_candidate_id
from research.wave39.wave39_engine_v4 import prior_atr_and_trends, simulate_stop_time_paths


base.prior_atr_and_trends = prior_atr_and_trends
base.simulate_stop_time_paths = simulate_stop_time_paths

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
SCORE_QUANTILES = (0.95, 0.975, 0.99)
VOLUME_QUANTILES = (0.90, 0.95)
WEIGHTS = (0.5, 1.0, 2.0)
TREND_MODES = ("none", "aligned_60", "opposed_60")
CLOCK_MODES = ("all", "minute_00")
HORIZONS = np.asarray((60, 120, 240, 480), dtype=np.int64)
STOPS = np.asarray((3.0, 4.0), dtype=np.float64)
LATENCIES = np.asarray((1, 2, 5), dtype=np.int64)
FAMILIES = (
    "IDIOSYNCRATIC_FLOW_FOLLOW",
    "FLOW_UNDERREACTION_CATCHUP",
    "PRICE_OVERREACTION_FADE",
    "BTC_TO_ALT_CATCHUP",
    "UNSUPPORTED_RELATIVE_DISLOCATION",
)
FOLD_EDGES_MS = tuple(
    (int(pd.Timestamp(start).timestamp() * 1000), int(pd.Timestamp(end).timestamp() * 1000))
    for start, end in (
        ("2022-01-01T00:00:00Z", "2022-05-01T00:00:00Z"),
        ("2022-05-01T00:00:00Z", "2022-09-01T00:00:00Z"),
        ("2022-09-01T00:00:00Z", "2023-01-01T00:00:00Z"),
    )
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--registration", type=Path, required=True)
    return parser.parse_args()


def rolling_z(values: np.ndarray) -> np.ndarray:
    series = pd.Series(values, dtype="float64")
    shifted = series.shift(1)
    mean = shifted.rolling(60 * 96, min_periods=30 * 96).mean()
    std = shifted.rolling(60 * 96, min_periods=30 * 96).std(ddof=0)
    return ((series - mean) / std.replace(0.0, np.nan)).to_numpy(dtype=np.float64)


def rolling_beta(target: np.ndarray, factor: np.ndarray) -> np.ndarray:
    target_series = pd.Series(target, dtype="float64")
    factor_series = pd.Series(factor, dtype="float64")
    target_prior = target_series.shift(1)
    factor_prior = factor_series.shift(1)
    mean_target = target_prior.rolling(60 * 96, min_periods=30 * 96).mean()
    mean_factor = factor_prior.rolling(60 * 96, min_periods=30 * 96).mean()
    covariance = ((target_prior - mean_target) * (factor_prior - mean_factor)).rolling(
        60 * 96, min_periods=30 * 96
    ).mean()
    variance = ((factor_prior - mean_factor) ** 2).rolling(
        60 * 96, min_periods=30 * 96
    ).mean()
    return (covariance / variance.replace(0.0, np.nan)).to_numpy(dtype=np.float64)


def prior_quantile(values: np.ndarray, quantile: float) -> np.ndarray:
    return (
        pd.Series(values, dtype="float64")
        .shift(1)
        .rolling(60 * 96, min_periods=30 * 96)
        .quantile(quantile)
        .to_numpy(dtype=np.float64)
    )


def build_cross_section(boundaries: dict[str, pd.DataFrame]) -> dict[str, np.ndarray]:
    clocks = [boundaries[symbol]["boundary_time_ms"].to_numpy(np.int64) for symbol in SYMBOLS]
    for clock in clocks[1:]:
        if not np.array_equal(clock, clocks[0]):
            raise RuntimeError("cross-sectional boundary clocks are not identical")
    imbalance = np.column_stack([
        boundaries[symbol]["post10s_imbalance"].to_numpy(np.float64) for symbol in SYMBOLS
    ])
    returns = np.column_stack([
        boundaries[symbol]["post10s_log_return"].to_numpy(np.float64) for symbol in SYMBOLS
    ])
    total_quote = np.column_stack([
        boundaries[symbol]["post10s_total_quote"].to_numpy(np.float64) for symbol in SYMBOLS
    ])
    flow_z = np.column_stack([rolling_z(imbalance[:, index]) for index in range(len(SYMBOLS))])
    return_z = np.column_stack([rolling_z(returns[:, index]) for index in range(len(SYMBOLS))])
    log_volume = np.log(np.maximum(total_quote, 1.0))
    volume_z = np.column_stack([rolling_z(log_volume[:, index]) for index in range(len(SYMBOLS))])

    residual_flow = np.full_like(flow_z, np.nan)
    residual_return = np.full_like(return_z, np.nan)
    residual_flow[:, 0] = flow_z[:, 0]
    residual_return[:, 0] = return_z[:, 0]
    for index in range(1, len(SYMBOLS)):
        beta_flow = rolling_beta(flow_z[:, index], flow_z[:, 0])
        beta_return = rolling_beta(return_z[:, index], return_z[:, 0])
        residual_flow[:, index] = flow_z[:, index] - beta_flow * flow_z[:, 0]
        residual_return[:, index] = return_z[:, index] - beta_return * return_z[:, 0]

    volume_thresholds: dict[float, np.ndarray] = {}
    for quantile in VOLUME_QUANTILES:
        volume_thresholds[quantile] = np.column_stack([
            prior_quantile(log_volume[:, index], quantile) for index in range(len(SYMBOLS))
        ])
    return {
        "clock": clocks[0],
        "imbalance": imbalance,
        "return10": returns,
        "total_quote": total_quote,
        "log_volume": log_volume,
        "flow_z": flow_z,
        "return_z": return_z,
        "volume_z": volume_z,
        "residual_flow": residual_flow,
        "residual_return": residual_return,
        "volume_thresholds": volume_thresholds,
    }


def choose_family(
    family: str,
    weight: float,
    state: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    flow = state["residual_flow"]
    ret = state["residual_return"]
    n, assets = flow.shape
    scores = np.full((n, assets), -np.inf, dtype=np.float64)
    sides = np.zeros((n, assets), dtype=np.int8)
    if family == "IDIOSYNCRATIC_FLOW_FOLLOW":
        valid = np.isfinite(flow) & np.isfinite(ret) & (flow * ret > 0.0)
        scores[valid] = np.abs(flow[valid])
        sides = np.sign(flow).astype(np.int8)
    elif family == "FLOW_UNDERREACTION_CATCHUP":
        raw = np.abs(flow) - weight * np.abs(ret)
        valid = np.isfinite(raw) & np.isfinite(flow) & (raw > 0.0)
        scores[valid] = raw[valid]
        sides = np.sign(flow).astype(np.int8)
    elif family == "PRICE_OVERREACTION_FADE":
        raw = np.abs(ret) - weight * np.abs(flow)
        valid = np.isfinite(raw) & np.isfinite(ret) & (raw > 0.0)
        scores[valid] = raw[valid]
        sides = -np.sign(ret).astype(np.int8)
    elif family == "BTC_TO_ALT_CATCHUP":
        btc_flow = flow[:, 0]
        btc_side = np.sign(btc_flow).astype(np.int8)
        for asset in range(1, assets):
            same_direction_response = btc_side.astype(np.float64) * ret[:, asset]
            own_flow_support = btc_side.astype(np.float64) * flow[:, asset]
            raw = np.abs(btc_flow) - weight * same_direction_response
            valid = (
                np.isfinite(raw) & np.isfinite(own_flow_support)
                & (btc_side != 0) & (own_flow_support >= -0.5)
            )
            scores[valid, asset] = raw[valid]
            sides[:, asset] = btc_side
    elif family == "UNSUPPORTED_RELATIVE_DISLOCATION":
        for asset in range(1, assets):
            return_side = np.sign(ret[:, asset]).astype(np.int8)
            signed_support = return_side.astype(np.float64) * flow[:, asset]
            raw = np.abs(ret[:, asset]) - weight * np.maximum(signed_support, 0.0)
            valid = (
                np.isfinite(raw) & np.isfinite(signed_support)
                & (return_side != 0) & (raw > 0.0)
                & (signed_support <= 0.5 * np.abs(ret[:, asset]))
            )
            scores[valid, asset] = raw[valid]
            sides[:, asset] = -return_side
    else:
        raise ValueError(family)

    chosen_asset = np.argmax(scores, axis=1).astype(np.int8)
    row_index = np.arange(n)
    chosen_score = scores[row_index, chosen_asset]
    chosen_side = sides[row_index, chosen_asset]
    invalid = ~np.isfinite(chosen_score) | (chosen_score == -np.inf) | (chosen_side == 0)
    chosen_asset[invalid] = -1
    chosen_side[invalid] = 0
    chosen_score[invalid] = np.nan
    return chosen_asset, chosen_side, chosen_score


def selected_vector(matrix: np.ndarray, selected_asset: np.ndarray) -> np.ndarray:
    result = np.full(len(selected_asset), np.nan, dtype=np.float64)
    valid = selected_asset >= 0
    rows = np.flatnonzero(valid)
    result[rows] = matrix[rows, selected_asset[rows]]
    return result


def selected_trend_mask(
    trends: dict[str, dict[int, np.ndarray]],
    mode: str,
    selected_asset: np.ndarray,
    side: np.ndarray,
) -> np.ndarray:
    if mode == "none":
        return selected_asset >= 0
    relation, minutes_text = mode.split("_")
    minute = int(minutes_text)
    chosen = np.full(len(selected_asset), np.nan)
    for asset, symbol in enumerate(SYMBOLS):
        mask = selected_asset == asset
        chosen[mask] = trends[symbol][minute][mask]
    aligned = side.astype(np.float64) * chosen > 0.0
    return aligned if relation == "aligned" else (~aligned & np.isfinite(chosen))


def gather_outcome(
    paths,
    selected_asset: np.ndarray,
    side: np.ndarray,
    horizon_index: int,
    stop_index: int,
    latency_index: int,
    cost_bp: float,
):
    n = len(selected_asset)
    net = np.full(n, np.nan)
    entry = np.full(n, -1, dtype=np.int64)
    exit_time = np.full(n, -1, dtype=np.int64)
    stopped = np.zeros(n, dtype=np.uint8)
    for asset, symbol in enumerate(SYMBOLS):
        mask = selected_asset == asset
        if not mask.any():
            continue
        symbol_net, symbol_entry, symbol_exit, symbol_stopped = paths[symbol].outcome(
            side=side,
            horizon_index=horizon_index,
            stop_index=stop_index,
            latency_index=latency_index,
            round_trip_bp=cost_bp,
        )
        net[mask] = symbol_net[mask]
        entry[mask] = symbol_entry[mask]
        exit_time[mask] = symbol_exit[mask]
        stopped[mask] = symbol_stopped[mask]
    return net, entry, exit_time, stopped


def development_gate(row: dict[str, Any]) -> bool:
    return bool(
        row["trades"] >= 90
        and row["net24"] > 0.0
        and row["net32"] > 0.0
        and row["positive_folds24"] == 3
        and row["positive_months24"] >= 9
        and row["pf24"] >= 1.15
        and row["top10_share24"] <= 0.40
        and row["net_after_top10_24"] > 0.0
        and row["latency2_net24"] > 0.0
    )


def neighborhood(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["neighborhood_size"] = 0
    result["neighborhood_positive_fraction"] = 0.0
    for index, row in result.loc[result["pre_neighborhood_gate"]].iterrows():
        peers = result[
            (result["family"] == row["family"])
            & (result["trend_mode"] == row["trend_mode"])
            & (result["clock_mode"] == row["clock_mode"])
        ]
        adjacent = (
            (np.abs(peers["score_q_index"].to_numpy(int) - int(row["score_q_index"])) <= 1)
            & (np.abs(peers["volume_q_index"].to_numpy(int) - int(row["volume_q_index"])) <= 1)
            & (np.abs(peers["weight_index"].to_numpy(int) - int(row["weight_index"])) <= 1)
            & (np.abs(peers["horizon_index"].to_numpy(int) - int(row["horizon_index"])) <= 1)
            & (np.abs(peers["stop_index"].to_numpy(int) - int(row["stop_index"])) <= 1)
        )
        peers = peers.loc[adjacent]
        if len(peers):
            positive = (peers["net32"] > 0.0) & (peers["min_fold24"] > 0.0)
            result.at[index, "neighborhood_size"] = int(len(peers))
            result.at[index, "neighborhood_positive_fraction"] = float(positive.mean())
    result["development_gate"] = (
        result["pre_neighborhood_gate"]
        & (result["neighborhood_size"] >= 6)
        & (result["neighborhood_positive_fraction"] >= 0.60)
    )
    return result


def main() -> int:
    args = arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    registration = json.loads(args.registration.read_text(encoding="utf-8"))
    if registration["registration_id"] != "wave41-cross-sectional-boundary-flow-v1":
        raise RuntimeError("registration identity mismatch")

    boundaries = {}
    paths = {}
    trends = {}
    input_hashes = {}
    for symbol in SYMBOLS:
        boundary = base.load_boundary(args.data_root, symbol)
        contract, funding = base.load_support(args.data_root, symbol)
        symbol_paths, symbol_trends = base.build_paths(symbol, boundary, contract, funding)
        boundaries[symbol] = boundary
        paths[symbol] = symbol_paths
        trends[symbol] = symbol_trends
        for path in (
            args.data_root / f"{symbol}_quarterhour_exact_2022.csv.gz",
            args.data_root / "support" / f"{symbol}_contract_1m_2022.csv.gz",
            args.data_root / "support" / f"{symbol}_funding_2022.csv.gz",
        ):
            input_hashes[str(path.relative_to(args.data_root))] = sha256_file(path)

    state = build_cross_section(boundaries)
    event_times = state["clock"]
    clock_masks = {mode: base.clock_mask(event_times, mode) for mode in CLOCK_MODES}
    rows: list[dict[str, Any]] = []
    choice_cache = {}

    for family in FAMILIES:
        for weight_index, weight in enumerate(WEIGHTS):
            selected_asset, side, score = choose_family(family, weight, state)
            score_thresholds = {
                quantile: prior_quantile(score, quantile) for quantile in SCORE_QUANTILES
            }
            choice_cache[(family, weight)] = (selected_asset, side, score, score_thresholds)
            chosen_volume = selected_vector(state["log_volume"], selected_asset)
            chosen_volume_thresholds = {
                quantile: selected_vector(state["volume_thresholds"][quantile], selected_asset)
                for quantile in VOLUME_QUANTILES
            }
            trend_masks = {
                mode: selected_trend_mask(trends, mode, selected_asset, side)
                for mode in TREND_MODES
            }
            for horizon_index, horizon in enumerate(HORIZONS):
                for stop_index, stop in enumerate(STOPS):
                    net24, entry1, exit1, _ = gather_outcome(
                        paths, selected_asset, side, horizon_index, stop_index, 0, 24.0
                    )
                    net32, _, _, _ = gather_outcome(
                        paths, selected_asset, side, horizon_index, stop_index, 0, 32.0
                    )
                    latency2_net, entry2, exit2, _ = gather_outcome(
                        paths, selected_asset, side, horizon_index, stop_index, 1, 24.0
                    )
                    for score_q_index, score_q in enumerate(SCORE_QUANTILES):
                        score_mask = score >= score_thresholds[score_q]
                        for volume_q_index, volume_q in enumerate(VOLUME_QUANTILES):
                            volume_mask = chosen_volume >= chosen_volume_thresholds[volume_q]
                            base_mask = (
                                (selected_asset >= 0) & score_mask & volume_mask
                                & np.isfinite(score) & np.isfinite(chosen_volume)
                            )
                            for trend_mode in TREND_MODES:
                                for clock_mode in CLOCK_MODES:
                                    eligible = (
                                        base_mask & trend_masks[trend_mode] & clock_masks[clock_mode]
                                        & np.isfinite(net24) & np.isfinite(net32)
                                    )
                                    selected = greedy_one_slot(np.flatnonzero(eligible), entry1, exit1)
                                    eligible2 = (
                                        base_mask & trend_masks[trend_mode] & clock_masks[clock_mode]
                                        & np.isfinite(latency2_net)
                                    )
                                    selected2 = greedy_one_slot(
                                        np.flatnonzero(eligible2), entry2, exit2
                                    )
                                    summary = base.fast_summary(
                                        selected, net24, net32, entry1, selected2, latency2_net
                                    )
                                    identity = {
                                        "family": family,
                                        "score_quantile": score_q,
                                        "volume_quantile": volume_q,
                                        "flow_price_weight": weight,
                                        "trend_mode": trend_mode,
                                        "clock_mode": clock_mode,
                                        "horizon_minutes": int(horizon),
                                        "stop_atr": float(stop),
                                        "base_latency_minutes": 1,
                                    }
                                    row = dict(identity)
                                    row["candidate_id"] = stable_candidate_id(identity)
                                    row.update(summary)
                                    row.update({
                                        "score_q_index": score_q_index,
                                        "volume_q_index": volume_q_index,
                                        "weight_index": weight_index,
                                        "horizon_index": horizon_index,
                                        "stop_index": stop_index,
                                    })
                                    row["pre_neighborhood_gate"] = development_gate(row)
                                    rows.append(row)

    frame = pd.DataFrame(rows)
    if frame["candidate_id"].duplicated().any():
        raise RuntimeError("candidate ID collision")
    frame = neighborhood(frame)
    frame.sort_values(
        ["development_gate", "min_fold24", "net32", "top10_share24"],
        ascending=[False, False, False, True],
        inplace=True,
        kind="mergesort",
    )
    frame.to_csv(
        args.output_dir / "wave41_all_candidates_2022.csv.gz",
        index=False,
        compression="gzip",
    )
    gated = frame.loc[frame["development_gate"]].copy()
    gated.to_csv(args.output_dir / "wave41_gated_candidates_2022.csv", index=False)

    selected_id = None
    if len(gated):
        selected = gated.iloc[0]
        family = str(selected["family"])
        weight = float(selected["flow_price_weight"])
        selected_asset, side, score, score_thresholds = choice_cache[(family, weight)]
        chosen_volume = selected_vector(state["log_volume"], selected_asset)
        chosen_volume_threshold = selected_vector(
            state["volume_thresholds"][float(selected["volume_quantile"])], selected_asset
        )
        mask = (
            (selected_asset >= 0)
            & (score >= score_thresholds[float(selected["score_quantile"])])
            & (chosen_volume >= chosen_volume_threshold)
            & selected_trend_mask(trends, str(selected["trend_mode"]), selected_asset, side)
            & clock_masks[str(selected["clock_mode"])]
        )
        hi = int(selected["horizon_index"])
        si = int(selected["stop_index"])
        ledgers = {}
        for cost in (18.0, 24.0, 32.0, 40.0):
            net, entry, exit_time, stopped = gather_outcome(
                paths, selected_asset, side, hi, si, 0, cost
            )
            chosen = greedy_one_slot(np.flatnonzero(mask & np.isfinite(net)), entry, exit_time)
            ledgers[int(cost)] = (net, entry, exit_time, stopped, chosen)
        base_net, base_entry, base_exit, _, chosen = ledgers[24]
        ledger_rows = []
        for index in chosen:
            row = {
                "event_index": int(index),
                "event_time_ms": int(event_times[index]),
                "entry_time_ms": int(base_entry[index]),
                "exit_time_ms": int(base_exit[index]),
                "symbol": SYMBOLS[int(selected_asset[index])],
                "side": int(side[index]),
                "score": float(score[index]),
            }
            for cost, values in ledgers.items():
                row[f"net_log_{cost}bp"] = float(values[0][index])
                row[f"stopped_{cost}bp"] = int(values[3][index])
            ledger_rows.append(row)
        ledger = pd.DataFrame(ledger_rows)
        ledger.to_csv(args.output_dir / "wave41_selected_ledger_2022.csv", index=False)
        opposite_net, opposite_entry, opposite_exit, _ = gather_outcome(
            paths, selected_asset, -side, hi, si, 0, 24.0
        )
        opposite = greedy_one_slot(
            np.flatnonzero(mask & np.isfinite(opposite_net)), opposite_entry, opposite_exit
        )
        audit = {
            "candidate_id": str(selected["candidate_id"]),
            "cost_metrics": {
                str(cost): metrics(
                    values[0][values[4]], values[1][values[4]], fold_edges_ms=FOLD_EDGES_MS
                )
                for cost, values in ledgers.items()
            },
            "opposite_direction_24bp": metrics(
                opposite_net[opposite], opposite_entry[opposite], fold_edges_ms=FOLD_EDGES_MS
            ),
        }
        payload = {
            "candidate": {
                key: (value.item() if isinstance(value, np.generic) else value)
                for key, value in selected.to_dict().items()
            },
            "audit": audit,
        }
        selected_id = str(selected["candidate_id"])
        (args.output_dir / "wave41_selected_candidate_2022.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )

    manifest = {
        "schema": "wave41-development-result-v1",
        "registration_sha256": sha256_file(args.registration),
        "input_hashes": input_hashes,
        "candidate_count": int(len(frame)),
        "pre_neighborhood_gate_count": int(frame["pre_neighborhood_gate"].sum()),
        "development_gate_count": int(frame["development_gate"].sum()),
        "selected_candidate_id": selected_id,
        "2023_opened": False,
        "2024_opened": False,
        "sealed_terminal_oos_opened": False,
        "risk_or_leverage_optimized": False,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

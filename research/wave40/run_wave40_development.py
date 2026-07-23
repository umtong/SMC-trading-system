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
HORIZONS = np.asarray((60, 120, 240, 480), dtype=np.int64)
STOPS = np.asarray((3.0, 4.0), dtype=np.float64)
LATENCIES = np.asarray((1, 2, 5), dtype=np.int64)
POST_Q = (0.95, 0.975, 0.99)
VOLUME_Q = (0.90, 0.95)
PRE_Q = (0.80, 0.90)
TOP_Q = (0.75, 0.90)
COUNT_Q = (0.90, 0.95)
DISAGREE_Q = (0.90, 0.95)
TREND_MODES = ("none", "aligned_60", "opposed_60")
CLOCK_MODES = ("all", "minute_00")
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


def prior_quantile(values: np.ndarray, quantile: float) -> np.ndarray:
    return (
        pd.Series(values, dtype="float64")
        .shift(1)
        .rolling(60 * 96, min_periods=30 * 96)
        .quantile(quantile)
        .to_numpy(dtype=np.float64)
    )


def components(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    buy_trades = frame["post10s_buy_trades"].to_numpy(dtype=np.float64)
    sell_trades = frame["post10s_sell_trades"].to_numpy(dtype=np.float64)
    trades = buy_trades + sell_trades
    count_imbalance = np.divide(
        buy_trades - sell_trades,
        trades,
        out=np.full(len(frame), np.nan),
        where=trades > 0.0,
    )
    dollar = frame["post10s_imbalance"].to_numpy(dtype=np.float64)
    result = {
        "dollar": dollar,
        "dollar_side": np.sign(dollar).astype(np.int8),
        "count": count_imbalance,
        "count_side": np.sign(count_imbalance).astype(np.int8),
        "disagreement": dollar - count_imbalance,
        "volume": frame["post10s_total_quote"].to_numpy(dtype=np.float64),
        "top5": frame["post10s_top5_share"].to_numpy(dtype=np.float64),
        "return10": frame["post10s_log_return"].to_numpy(dtype=np.float64),
        "pre60": frame["pre60s_imbalance"].to_numpy(dtype=np.float64),
        "pre60_side": np.sign(frame["pre60s_imbalance"].to_numpy(dtype=np.float64)).astype(np.int8),
        "pre_complete": frame["pre_clock_complete"].to_numpy(dtype=np.int8) == 1,
    }
    return result


def thresholds(values: dict[str, np.ndarray]) -> dict[tuple[str, float], np.ndarray]:
    result: dict[tuple[str, float], np.ndarray] = {}
    for quantile in POST_Q:
        result[("post", quantile)] = prior_quantile(np.abs(values["dollar"]), quantile)
    for quantile in VOLUME_Q:
        result[("volume", quantile)] = prior_quantile(values["volume"], quantile)
    for quantile in PRE_Q:
        result[("pre", quantile)] = prior_quantile(np.abs(values["pre60"]), quantile)
    for quantile in TOP_Q:
        result[("top", quantile)] = prior_quantile(values["top5"], quantile)
    for quantile in COUNT_Q:
        result[("count", quantile)] = prior_quantile(np.abs(values["count"]), quantile)
    for quantile in DISAGREE_Q:
        result[("disagreement", quantile)] = prior_quantile(
            np.abs(values["disagreement"]), quantile
        )
    result[("post", 0.50)] = prior_quantile(np.abs(values["dollar"]), 0.50)
    return result


def family_parameter_sets(family: str):
    if family in ("PRE_POST_PERSISTENCE", "PRE_POST_REVERSAL"):
        for post_q in POST_Q:
            for volume_q in VOLUME_Q:
                for pre_q in PRE_Q:
                    yield {
                        "post_quantile": post_q,
                        "volume_quantile": volume_q,
                        "pre_quantile": pre_q,
                        "top_quantile": None,
                        "count_quantile": None,
                        "disagreement_quantile": None,
                        "c0": POST_Q.index(post_q),
                        "c1": VOLUME_Q.index(volume_q),
                        "c2": PRE_Q.index(pre_q),
                        "c3": -1,
                    }
    elif family in ("BLOCK_SIZE_CONTINUATION", "BLOCK_SIZE_ABSORPTION"):
        for post_q in POST_Q:
            for volume_q in VOLUME_Q:
                for top_q in TOP_Q:
                    for disagreement_q in DISAGREE_Q:
                        yield {
                            "post_quantile": post_q,
                            "volume_quantile": volume_q,
                            "pre_quantile": None,
                            "top_quantile": top_q,
                            "count_quantile": None,
                            "disagreement_quantile": disagreement_q,
                            "c0": POST_Q.index(post_q),
                            "c1": VOLUME_Q.index(volume_q),
                            "c2": TOP_Q.index(top_q),
                            "c3": DISAGREE_Q.index(disagreement_q),
                        }
    elif family == "RETAIL_COUNT_FADE":
        for count_q in COUNT_Q:
            for volume_q in VOLUME_Q:
                yield {
                    "post_quantile": None,
                    "volume_quantile": volume_q,
                    "pre_quantile": None,
                    "top_quantile": None,
                    "count_quantile": count_q,
                    "disagreement_quantile": None,
                    "c0": COUNT_Q.index(count_q),
                    "c1": VOLUME_Q.index(volume_q),
                    "c2": -1,
                    "c3": -1,
                }
    else:
        raise ValueError(family)


def family_mask_side(
    family: str,
    params: dict[str, Any],
    value: dict[str, np.ndarray],
    threshold: dict[tuple[str, float], np.ndarray],
) -> tuple[np.ndarray, np.ndarray, str]:
    volume_q = float(params["volume_quantile"])
    active_volume = value["volume"] >= threshold[("volume", volume_q)]
    if family in ("PRE_POST_PERSISTENCE", "PRE_POST_REVERSAL"):
        post_q = float(params["post_quantile"])
        pre_q = float(params["pre_quantile"])
        post_side = value["dollar_side"]
        pre_side = value["pre60_side"]
        extreme = (
            np.abs(value["dollar"]) >= threshold[("post", post_q)]
        ) & (
            np.abs(value["pre60"]) >= threshold[("pre", pre_q)]
        )
        same = post_side == pre_side
        accepted = post_side.astype(float) * value["return10"] > 0.0
        relation = same if family == "PRE_POST_PERSISTENCE" else (~same)
        mask = (
            value["pre_complete"] & active_volume & extreme & relation
            & (post_side != 0) & (pre_side != 0) & accepted
        )
        return mask, post_side, "dollar"
    if family in ("BLOCK_SIZE_CONTINUATION", "BLOCK_SIZE_ABSORPTION"):
        post_q = float(params["post_quantile"])
        top_q = float(params["top_quantile"])
        disagreement_q = float(params["disagreement_quantile"])
        side = value["dollar_side"]
        concentrated = value["top5"] >= threshold[("top", top_q)]
        size_dominant = (
            np.abs(value["disagreement"]) >= threshold[("disagreement", disagreement_q)]
        ) & (side.astype(float) * value["disagreement"] > 0.0) & (
            np.abs(value["count"]) < np.abs(value["dollar"])
        )
        extreme = np.abs(value["dollar"]) >= threshold[("post", post_q)]
        accepted = side.astype(float) * value["return10"] > 0.0
        relation = accepted if family == "BLOCK_SIZE_CONTINUATION" else (~accepted & np.isfinite(value["return10"]))
        mask = active_volume & concentrated & size_dominant & extreme & relation & (side != 0)
        output_side = side if family == "BLOCK_SIZE_CONTINUATION" else -side
        return mask, output_side, "dollar" if family == "BLOCK_SIZE_CONTINUATION" else "fade_dollar"
    if family == "RETAIL_COUNT_FADE":
        count_q = float(params["count_quantile"])
        count_side = value["count_side"]
        count_extreme = np.abs(value["count"]) >= threshold[("count", count_q)]
        dollar_weak = np.abs(value["dollar"]) <= threshold[("post", 0.50)]
        unconfirmed = (value["dollar"] * value["count"] <= 0.0) | (
            np.abs(value["dollar"]) <= 0.5 * np.abs(value["count"])
        )
        moved_price = count_side.astype(float) * value["return10"] > 0.0
        mask = active_volume & count_extreme & dollar_weak & unconfirmed & moved_price & (count_side != 0)
        return mask, -count_side, "fade_count"
    raise ValueError(family)


def precompute_outcomes(paths, sides: dict[str, np.ndarray]):
    cache = {}
    for side_key, side in sides.items():
        for hi in range(len(HORIZONS)):
            for si in range(len(STOPS)):
                for cost, latency_index in ((24.0, 0), (32.0, 0), (24.0, 1), (40.0, 0), (18.0, 0)):
                    key = (side_key, hi, si, int(cost), latency_index)
                    cache[key] = paths.outcome(
                        side=side,
                        horizon_index=hi,
                        stop_index=si,
                        latency_index=latency_index,
                        round_trip_bp=cost,
                    )
    return cache


def development_gate(row: dict[str, Any]) -> bool:
    return bool(
        row["trades"] >= 70
        and row["net24"] > 0.0
        and row["net32"] > 0.0
        and row["positive_folds24"] == 3
        and row["positive_months24"] >= 8
        and row["pf24"] >= 1.15
        and row["top10_share24"] <= 0.40
        and row["net_after_top10_24"] > 0.0
        and row["latency2_net24"] > 0.0
    )


def evaluate_symbol(symbol: str, frame: pd.DataFrame, paths, trends) -> list[dict[str, Any]]:
    value = components(frame)
    threshold = thresholds(value)
    sides = {
        "dollar": value["dollar_side"],
        "fade_dollar": -value["dollar_side"],
        "fade_count": -value["count_side"],
    }
    outcome = precompute_outcomes(paths, sides)
    event_times = frame["boundary_time_ms"].to_numpy(dtype=np.int64)
    rows: list[dict[str, Any]] = []
    for family in (
        "PRE_POST_PERSISTENCE", "PRE_POST_REVERSAL",
        "BLOCK_SIZE_CONTINUATION", "BLOCK_SIZE_ABSORPTION", "RETAIL_COUNT_FADE",
    ):
        for params in family_parameter_sets(family):
            base_mask, side, side_key = family_mask_side(family, params, value, threshold)
            for hi, horizon in enumerate(HORIZONS):
                for si, stop in enumerate(STOPS):
                    net24, entry1, exit1, _ = outcome[(side_key, hi, si, 24, 0)]
                    net32, _, _, _ = outcome[(side_key, hi, si, 32, 0)]
                    latency2_net, entry2, exit2, _ = outcome[(side_key, hi, si, 24, 1)]
                    for trend_mode in TREND_MODES:
                        trend = base.trend_mask(trends, trend_mode, side)
                        for clock_mode in CLOCK_MODES:
                            eligible = (
                                base_mask & trend & base.clock_mask(event_times, clock_mode)
                                & np.isfinite(net24) & np.isfinite(net32)
                            )
                            selected = greedy_one_slot(np.flatnonzero(eligible), entry1, exit1)
                            eligible2 = (
                                base_mask & trend & base.clock_mask(event_times, clock_mode)
                                & np.isfinite(latency2_net)
                            )
                            selected2 = greedy_one_slot(np.flatnonzero(eligible2), entry2, exit2)
                            summary = base.fast_summary(
                                selected, net24, net32, entry1, selected2, latency2_net
                            )
                            identity = {
                                "symbol": symbol,
                                "family": family,
                                **{key: value_ for key, value_ in params.items() if not key.startswith("c")},
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
                                "c0": params["c0"], "c1": params["c1"],
                                "c2": params["c2"], "c3": params["c3"],
                                "h_idx": hi, "s_idx": si,
                            })
                            row["pre_neighborhood_gate"] = development_gate(row)
                            rows.append(row)
    return rows


def neighborhood(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["neighborhood_size"] = 0
    result["neighborhood_positive_fraction"] = 0.0
    for index, row in result.loc[result["pre_neighborhood_gate"]].iterrows():
        peers = result[
            (result["symbol"] == row["symbol"])
            & (result["family"] == row["family"])
            & (result["trend_mode"] == row["trend_mode"])
            & (result["clock_mode"] == row["clock_mode"])
        ]
        coordinate_mask = np.ones(len(peers), dtype=bool)
        for column in ("c0", "c1", "c2", "c3", "h_idx", "s_idx"):
            coordinate_mask &= np.abs(peers[column].to_numpy(int) - int(row[column])) <= 1
        peers = peers.loc[coordinate_mask]
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


def reconstruct(selected: pd.Series, frame: pd.DataFrame, paths, trends):
    value = components(frame); threshold = thresholds(value)
    params = {key: selected[key] for key in (
        "post_quantile", "volume_quantile", "pre_quantile", "top_quantile",
        "count_quantile", "disagreement_quantile",
    )}
    for key, value_ in list(params.items()):
        if pd.isna(value_): params[key] = None
    mask, side, _ = family_mask_side(str(selected["family"]), params, value, threshold)
    mask &= base.trend_mask(trends, str(selected["trend_mode"]), side)
    event_times = frame["boundary_time_ms"].to_numpy(np.int64)
    mask &= base.clock_mask(event_times, str(selected["clock_mode"]))
    hi = int(np.where(HORIZONS == int(selected["horizon_minutes"]))[0][0])
    si = int(np.where(STOPS == float(selected["stop_atr"]))[0][0])
    ledgers = {}
    for cost in (18.0, 24.0, 32.0, 40.0):
        net, entry, exit_time, stopped = paths.outcome(
            side=side, horizon_index=hi, stop_index=si, latency_index=0,
            round_trip_bp=cost,
        )
        chosen = greedy_one_slot(np.flatnonzero(mask & np.isfinite(net)), entry, exit_time)
        ledgers[int(cost)] = (net, entry, exit_time, stopped, chosen)
    base_net, base_entry, base_exit, _, chosen = ledgers[24]
    rows = []
    for index in chosen:
        row = {
            "event_index": int(index), "event_time_ms": int(event_times[index]),
            "entry_time_ms": int(base_entry[index]), "exit_time_ms": int(base_exit[index]),
            "side": int(side[index]), "symbol": str(selected["symbol"]),
        }
        for cost, values in ledgers.items():
            row[f"net_log_{cost}bp"] = float(values[0][index])
            row[f"stopped_{cost}bp"] = int(values[3][index])
        rows.append(row)
    opposite_net, opposite_entry, opposite_exit, _ = paths.outcome(
        side=-side, horizon_index=hi, stop_index=si, latency_index=0, round_trip_bp=24.0
    )
    opposite = greedy_one_slot(np.flatnonzero(mask & np.isfinite(opposite_net)), opposite_entry, opposite_exit)
    audit = {
        "candidate_id": str(selected["candidate_id"]),
        "cost_metrics": {
            str(cost): metrics(values[0][values[4]], values[1][values[4]], fold_edges_ms=FOLD_EDGES_MS)
            for cost, values in ledgers.items()
        },
        "opposite_direction_24bp": metrics(
            opposite_net[opposite], opposite_entry[opposite], fold_edges_ms=FOLD_EDGES_MS
        ),
    }
    return pd.DataFrame(rows), audit


def main() -> int:
    args = arguments(); args.output_dir.mkdir(parents=True, exist_ok=True)
    registration = json.loads(args.registration.read_text(encoding="utf-8"))
    if registration["registration_id"] != "wave40-prepost-size-disagreement-v1":
        raise RuntimeError("registration mismatch")
    rows = []; state = {}; input_hashes = {}
    for symbol in SYMBOLS:
        frame = base.load_boundary(args.data_root, symbol)
        contract, funding = base.load_support(args.data_root, symbol)
        paths, trends = base.build_paths(symbol, frame, contract, funding)
        state[symbol] = (frame, paths, trends)
        rows.extend(evaluate_symbol(symbol, frame, paths, trends))
        for path in (
            args.data_root / f"{symbol}_quarterhour_exact_2022.csv.gz",
            args.data_root / "support" / f"{symbol}_contract_1m_2022.csv.gz",
            args.data_root / "support" / f"{symbol}_funding_2022.csv.gz",
        ):
            input_hashes[str(path.relative_to(args.data_root))] = sha256_file(path)
    frame = pd.DataFrame(rows)
    if frame["candidate_id"].duplicated().any():
        raise RuntimeError("candidate ID collision")
    frame = neighborhood(frame)
    frame.sort_values(
        ["development_gate", "min_fold24", "net32", "top10_share24"],
        ascending=[False, False, False, True], kind="mergesort", inplace=True,
    )
    all_path = args.output_dir / "wave40_all_candidates_2022.csv.gz"
    frame.to_csv(all_path, index=False, compression="gzip")
    gated = frame.loc[frame["development_gate"]].copy()
    gated.to_csv(args.output_dir / "wave40_gated_candidates_2022.csv", index=False)
    selected_id = None
    if len(gated):
        selected = gated.iloc[0]
        symbol = str(selected["symbol"])
        ledger, audit = reconstruct(selected, *state[symbol])
        ledger.to_csv(args.output_dir / "wave40_selected_ledger_2022.csv", index=False)
        payload = {
            "candidate": {
                key: (value_.item() if isinstance(value_, np.generic) else value_)
                for key, value_ in selected.to_dict().items()
            },
            "audit": audit,
        }
        selected_id = str(selected["candidate_id"])
        (args.output_dir / "wave40_selected_candidate_2022.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
    manifest = {
        "schema": "wave40-development-result-v1",
        "registration_sha256": sha256_file(args.registration),
        "input_hashes": input_hashes,
        "candidate_count": int(len(frame)),
        "pre_neighborhood_gate_count": int(frame["pre_neighborhood_gate"].sum()),
        "development_gate_count": int(frame["development_gate"].sum()),
        "selected_candidate_id": selected_id,
        "2023_opened": False, "2024_opened": False,
        "sealed_terminal_oos_opened": False, "risk_or_leverage_optimized": False,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

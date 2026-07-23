from __future__ import annotations

import argparse
from dataclasses import asdict
import gzip
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.wave39.wave39_engine import (
    SymbolPaths,
    greedy_one_slot,
    metrics,
    sha256_file,
    simulate_stop_time_paths,
    stable_candidate_id,
)


SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
WINDOWS = (10, 30, 60)
IMBALANCE_QUANTILES = (0.95, 0.975, 0.99)
VOLUME_QUANTILES = (0.90, 0.95)
HORIZONS = np.asarray((60, 120, 240, 480, 720), dtype=np.int64)
STOPS = np.asarray((3.0, 4.0), dtype=np.float64)
LATENCIES = np.asarray((1, 2, 5), dtype=np.int64)
TREND_MODES = (
    "none",
    "aligned_15",
    "opposed_15",
    "aligned_60",
    "opposed_60",
    "aligned_240",
    "opposed_240",
)
CLOCK_MODES = ("all", "minute_00", "minute_15", "minute_30", "minute_45")
FOLD_EDGES = (
    (pd.Timestamp("2022-01-01", tz="UTC"), pd.Timestamp("2022-05-01", tz="UTC")),
    (pd.Timestamp("2022-05-01", tz="UTC"), pd.Timestamp("2022-09-01", tz="UTC")),
    (pd.Timestamp("2022-09-01", tz="UTC"), pd.Timestamp("2023-01-01", tz="UTC")),
)
FOLD_EDGES_MS = tuple((int(a.timestamp() * 1000), int(b.timestamp() * 1000)) for a, b in FOLD_EDGES)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--registration", type=Path, required=True)
    return parser.parse_args()


def rolling_prior_quantile(values: np.ndarray, quantile: float) -> np.ndarray:
    series = pd.Series(values, dtype="float64")
    return (
        series.shift(1)
        .rolling(window=60 * 96, min_periods=30 * 96)
        .quantile(quantile)
        .to_numpy(dtype=np.float64)
    )


def load_boundary(data_root: Path, symbol: str) -> pd.DataFrame:
    path = data_root / f"{symbol}_quarterhour_exact_2022.csv.gz"
    frame = pd.read_csv(path)
    expected = 365 * 96
    if len(frame) != expected:
        raise RuntimeError(f"{symbol}: boundary rows {len(frame)} != {expected}")
    times = frame["boundary_time_ms"].to_numpy(dtype=np.int64)
    if np.any(np.diff(times) != 900_000):
        raise RuntimeError(f"{symbol}: non-contiguous boundary clock")
    if frame["symbol"].nunique() != 1 or frame["symbol"].iloc[0] != symbol:
        raise RuntimeError(f"{symbol}: symbol identity mismatch")
    return frame


def load_support(data_root: Path, symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    support = data_root / "support"
    contract = pd.read_csv(support / f"{symbol}_contract_1m_2022.csv.gz")
    funding = pd.read_csv(support / f"{symbol}_funding_2022.csv.gz")
    if len(contract) != 365 * 1440:
        raise RuntimeError(f"{symbol}: contract minute count mismatch")
    minute_time = contract["open_time_ms"].to_numpy(dtype=np.int64)
    if np.any(np.diff(minute_time) != 60_000):
        raise RuntimeError(f"{symbol}: minute clock gap")
    return contract, funding


def prior_atr_and_trends(
    contract: pd.DataFrame,
    boundary_time_ms: np.ndarray,
) -> tuple[np.ndarray, dict[int, np.ndarray], np.ndarray]:
    minute_time = contract["open_time_ms"].to_numpy(dtype=np.int64)
    start = int(minute_time[0])
    boundary_index = ((boundary_time_ms - start) // 60_000).astype(np.int64)
    if np.any(minute_time[boundary_index] != boundary_time_ms):
        raise RuntimeError("boundary clock does not align to support minute clock")

    open_price = contract["open"].to_numpy(dtype=np.float64)
    high_price = contract["high"].to_numpy(dtype=np.float64)
    low_price = contract["low"].to_numpy(dtype=np.float64)
    close_price = contract["close"].to_numpy(dtype=np.float64)
    block_count = len(contract) // 15
    bar_open = open_price[: block_count * 15].reshape(block_count, 15)[:, 0]
    bar_high = high_price[: block_count * 15].reshape(block_count, 15).max(axis=1)
    bar_low = low_price[: block_count * 15].reshape(block_count, 15).min(axis=1)
    bar_close = close_price[: block_count * 15].reshape(block_count, 15)[:, -1]
    previous_close = np.concatenate(([np.nan], bar_close[:-1]))
    true_range = np.maximum(
        bar_high - bar_low,
        np.maximum(np.abs(bar_high - previous_close), np.abs(bar_low - previous_close)),
    )
    atr = pd.Series(true_range).rolling(14, min_periods=14).mean().to_numpy(dtype=np.float64)
    previous_bar = boundary_index // 15 - 1
    atr_at_boundary = np.full(len(boundary_index), np.nan)
    valid_bar = (previous_bar >= 0) & (previous_bar < len(atr))
    atr_at_boundary[valid_bar] = atr[previous_bar[valid_bar]]

    trends: dict[int, np.ndarray] = {}
    last_completed = boundary_index - 1
    for minutes in (15, 60, 240):
        earlier = last_completed - minutes
        values = np.full(len(boundary_index), np.nan)
        valid = earlier >= 0
        values[valid] = np.log(close_price[last_completed[valid]] / close_price[earlier[valid]])
        trends[minutes] = values
    return atr_at_boundary, trends, boundary_index


def build_paths(
    symbol: str,
    boundary: pd.DataFrame,
    contract: pd.DataFrame,
    funding: pd.DataFrame,
) -> tuple[SymbolPaths, dict[int, np.ndarray]]:
    event_time = boundary["boundary_time_ms"].to_numpy(dtype=np.int64)
    atr, trends, boundary_index = prior_atr_and_trends(contract, event_time)
    open_price = contract["open"].to_numpy(dtype=np.float64)
    high_price = contract["high"].to_numpy(dtype=np.float64)
    low_price = contract["low"].to_numpy(dtype=np.float64)
    minute_time = contract["open_time_ms"].to_numpy(dtype=np.int64)
    gross, exits, stopped, entry_index, entry_value = simulate_stop_time_paths(
        open_price,
        high_price,
        low_price,
        boundary_index,
        atr,
        HORIZONS,
        STOPS,
        LATENCIES,
    )
    funding_time = funding["funding_time_ms"].to_numpy(dtype=np.int64)
    funding_rate = funding["funding_rate"].to_numpy(dtype=np.float64)
    order = np.argsort(funding_time, kind="mergesort")
    funding_time = funding_time[order]
    funding_rate = funding_rate[order]
    if len(np.unique(funding_time)) != len(funding_time):
        raise RuntimeError(f"{symbol}: duplicate funding timestamps")
    paths = SymbolPaths(
        symbol=symbol,
        event_time_ms=event_time,
        boundary_minute_index=boundary_index,
        entry_time_ms=event_time + 60_000,
        gross=gross,
        exit_index=exits,
        stopped=stopped,
        entry_index=entry_index,
        entry_value=entry_value,
        minute_time_ms=minute_time,
        funding_time_ms=funding_time,
        funding_rate=funding_rate,
        horizons=HORIZONS,
        stops=STOPS,
        latencies=LATENCIES,
    )
    return paths, trends


def clock_mask(event_time_ms: np.ndarray, mode: str) -> np.ndarray:
    if mode == "all":
        return np.ones(len(event_time_ms), dtype=bool)
    minute = pd.to_datetime(event_time_ms, unit="ms", utc=True).minute.to_numpy()
    expected = int(mode.split("_")[1])
    return minute == expected


def trend_mask(trends: dict[int, np.ndarray], mode: str, side: np.ndarray) -> np.ndarray:
    if mode == "none":
        return np.ones(len(side), dtype=bool)
    relation, minutes_text = mode.split("_")
    trend = trends[int(minutes_text)]
    aligned = side.astype(np.float64) * trend > 0.0
    return aligned if relation == "aligned" else (~aligned & np.isfinite(trend))


def fast_summary(
    selected: np.ndarray,
    net24: np.ndarray,
    net32: np.ndarray,
    entry_time: np.ndarray,
    latency2_selected: np.ndarray,
    latency2_net24: np.ndarray,
) -> dict[str, Any]:
    values24 = net24[selected]
    values32 = net32[selected]
    times = entry_time[selected]
    positive = values24[values24 > 0.0]
    negative = values24[values24 < 0.0]
    pf = float(positive.sum() / -negative.sum()) if len(negative) else float("inf")
    winners = np.sort(positive)[::-1]
    winner_sum = float(winners.sum())
    top10 = float(winners[:10].sum())
    equity = np.exp(np.cumsum(values24)) if len(values24) else np.asarray([], dtype=float)
    path = np.concatenate(([1.0], equity))
    peaks = np.maximum.accumulate(path)
    mdd = float(np.max(1.0 - path / peaks)) if len(path) else 0.0
    folds = []
    for start, end in FOLD_EDGES_MS:
        mask = (times >= start) & (times < end)
        folds.append(float(values24[mask].sum()))
    month = pd.to_datetime(times, unit="ms", utc=True).month.to_numpy() - 1 if len(times) else np.asarray([], dtype=int)
    monthly = np.bincount(month, weights=values24, minlength=12) if len(month) else np.zeros(12)
    return {
        "trades": int(len(selected)),
        "net24": float(values24.sum()),
        "net32": float(values32.sum()),
        "pf24": pf,
        "mdd24": mdd,
        "positive_folds24": int(sum(value > 0.0 for value in folds)),
        "min_fold24": float(min(folds)) if folds else float("nan"),
        "fold0_24": folds[0] if folds else float("nan"),
        "fold1_24": folds[1] if folds else float("nan"),
        "fold2_24": folds[2] if folds else float("nan"),
        "positive_months24": int((monthly > 0.0).sum()),
        "worst_month24": float(monthly.min()),
        "top10_share24": top10 / winner_sum if winner_sum > 0.0 else float("nan"),
        "net_after_top10_24": float(values24.sum() - top10),
        "latency2_trades": int(len(latency2_selected)),
        "latency2_net24": float(latency2_net24[latency2_selected].sum()),
    }


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


def signal_components(boundary: pd.DataFrame) -> dict[str, dict[int, np.ndarray]]:
    result: dict[str, dict[int, np.ndarray]] = {
        "imbalance": {}, "total": {}, "net": {}, "return": {}, "last": {}
    }
    for window in WINDOWS:
        result["imbalance"][window] = boundary[f"post{window}s_imbalance"].to_numpy(dtype=np.float64)
        result["total"][window] = boundary[f"post{window}s_total_quote"].to_numpy(dtype=np.float64)
        result["net"][window] = boundary[f"post{window}s_net_quote"].to_numpy(dtype=np.float64)
        result["return"][window] = boundary[f"post{window}s_log_return"].to_numpy(dtype=np.float64)
        result["last"][window] = boundary[f"post{window}s_last_price"].to_numpy(dtype=np.float64)
    return result


def build_thresholds(components: dict[str, dict[int, np.ndarray]]) -> dict[tuple[str, int, float], np.ndarray]:
    thresholds: dict[tuple[str, int, float], np.ndarray] = {}
    for window in WINDOWS:
        for quantile in IMBALANCE_QUANTILES:
            thresholds[("imbalance", window, quantile)] = rolling_prior_quantile(
                np.abs(components["imbalance"][window]), quantile
            )
        for quantile in VOLUME_QUANTILES:
            thresholds[("volume", window, quantile)] = rolling_prior_quantile(
                components["total"][window], quantile
            )
    return thresholds


def candidate_mask_and_side(
    *,
    family: str,
    window: int,
    components: dict[str, dict[int, np.ndarray]],
    thresholds: dict[tuple[str, int, float], np.ndarray],
    q_imbalance: float,
    q_volume: float,
) -> tuple[np.ndarray, np.ndarray]:
    imbalance = components["imbalance"][window]
    total = components["total"][window]
    initial_side = np.sign(imbalance).astype(np.int8)
    extreme = (
        np.abs(imbalance) >= thresholds[("imbalance", window, q_imbalance)]
    ) & (total >= thresholds[("volume", window, q_volume)])
    alignment = initial_side.astype(np.float64) * components["return"][window]
    if family == "WITHIN_ASSET_BOUNDARY_CONTINUATION":
        side = initial_side
        mask = extreme & (alignment > 0.0)
    elif family == "WITHIN_ASSET_BOUNDARY_ABSORPTION":
        side = -initial_side
        mask = extreme & (alignment <= 0.0) & np.isfinite(alignment)
    elif family == "BOUNDARY_FLOW_FLIP":
        if window != 60:
            raise ValueError("flow flip is defined at 60 seconds")
        first_net = components["net"][10]
        incremental_net = components["net"][60] - first_net
        side = np.sign(incremental_net).astype(np.int8)
        first_side = np.sign(first_net).astype(np.int8)
        first_last = components["last"][10]
        final_last = components["last"][60]
        incremental_return = np.log(final_last / first_last)
        mask = (
            extreme
            & (side != 0)
            & (first_side != 0)
            & (side == -first_side)
            & (side.astype(np.float64) * incremental_return > 0.0)
        )
    else:
        raise ValueError(family)
    return mask & (side != 0), side


def evaluate_family_grid(
    *,
    source_symbol: str,
    trade_symbol: str,
    family: str,
    windows: tuple[int, ...],
    boundary: pd.DataFrame,
    components: dict[str, dict[int, np.ndarray]],
    thresholds: dict[tuple[str, int, float], np.ndarray],
    paths: SymbolPaths,
    trade_trends: dict[int, np.ndarray],
    reduced_cross_asset_grid: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    event_times = boundary["boundary_time_ms"].to_numpy(dtype=np.int64)
    q_imbalances = (0.975, 0.99) if reduced_cross_asset_grid else IMBALANCE_QUANTILES
    q_volumes = (0.95,) if reduced_cross_asset_grid else VOLUME_QUANTILES
    trend_modes = ("none", "aligned_60", "opposed_60") if reduced_cross_asset_grid else TREND_MODES
    horizon_values = (120, 240, 480) if reduced_cross_asset_grid else tuple(int(item) for item in HORIZONS)
    stop_values = (4.0,) if reduced_cross_asset_grid else tuple(float(item) for item in STOPS)
    clock_modes = ("all",) if reduced_cross_asset_grid else CLOCK_MODES

    for window in windows:
        for q_imbalance in q_imbalances:
            for q_volume in q_volumes:
                base_mask, side = candidate_mask_and_side(
                    family=family,
                    window=window,
                    components=components,
                    thresholds=thresholds,
                    q_imbalance=q_imbalance,
                    q_volume=q_volume,
                )
                for horizon in horizon_values:
                    hi = int(np.where(HORIZONS == horizon)[0][0])
                    for stop in stop_values:
                        si = int(np.where(STOPS == stop)[0][0])
                        net24, entry1, exit1, _ = paths.outcome(
                            side=side,
                            horizon_index=hi,
                            stop_index=si,
                            latency_index=0,
                            round_trip_bp=24.0,
                        )
                        net32, _, _, _ = paths.outcome(
                            side=side,
                            horizon_index=hi,
                            stop_index=si,
                            latency_index=0,
                            round_trip_bp=32.0,
                        )
                        latency2_net24, entry2, exit2, _ = paths.outcome(
                            side=side,
                            horizon_index=hi,
                            stop_index=si,
                            latency_index=1,
                            round_trip_bp=24.0,
                        )
                        finite1 = np.isfinite(net24) & np.isfinite(net32)
                        finite2 = np.isfinite(latency2_net24)
                        for trend_mode in trend_modes:
                            trend = trend_mask(trade_trends, trend_mode, side)
                            for clock_mode in clock_modes:
                                eligible = base_mask & trend & clock_mask(event_times, clock_mode) & finite1
                                selected = greedy_one_slot(np.flatnonzero(eligible), entry1, exit1)
                                eligible2 = base_mask & trend & clock_mask(event_times, clock_mode) & finite2
                                selected2 = greedy_one_slot(np.flatnonzero(eligible2), entry2, exit2)
                                params: dict[str, Any] = {
                                    "source_symbol": source_symbol,
                                    "trade_symbol": trade_symbol,
                                    "family": family,
                                    "window_seconds": window,
                                    "imbalance_quantile": q_imbalance,
                                    "volume_quantile": q_volume,
                                    "trend_mode": trend_mode,
                                    "clock_mode": clock_mode,
                                    "horizon_minutes": horizon,
                                    "stop_atr": stop,
                                    "base_latency_minutes": 1,
                                }
                                row = dict(params)
                                row["candidate_id"] = stable_candidate_id(params)
                                row.update(fast_summary(
                                    selected,
                                    net24,
                                    net32,
                                    entry1,
                                    selected2,
                                    latency2_net24,
                                ))
                                row["pre_neighborhood_gate"] = development_gate(row)
                                rows.append(row)
    return rows


def add_neighborhood_scores(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["neighborhood_positive_fraction"] = 0.0
    result["neighborhood_size"] = 0
    q_index = {value: index for index, value in enumerate(IMBALANCE_QUANTILES)}
    v_index = {value: index for index, value in enumerate(VOLUME_QUANTILES)}
    h_index = {int(value): index for index, value in enumerate(HORIZONS)}
    for index, row in result.loc[result["pre_neighborhood_gate"]].iterrows():
        peers = result[
            (result["source_symbol"] == row["source_symbol"])
            & (result["trade_symbol"] == row["trade_symbol"])
            & (result["family"] == row["family"])
            & (result["window_seconds"] == row["window_seconds"])
            & (result["trend_mode"] == row["trend_mode"])
            & (result["clock_mode"] == row["clock_mode"])
        ].copy()
        qi = q_index.get(float(row["imbalance_quantile"]), 1)
        vi = v_index.get(float(row["volume_quantile"]), 1)
        hi = h_index[int(row["horizon_minutes"])]
        peers = peers[
            peers["imbalance_quantile"].map(lambda value: abs(q_index.get(float(value), qi) - qi) <= 1)
            & peers["volume_quantile"].map(lambda value: abs(v_index.get(float(value), vi) - vi) <= 1)
            & peers["horizon_minutes"].map(lambda value: abs(h_index[int(value)] - hi) <= 1)
        ]
        if len(peers):
            positive = (peers["net32"] > 0.0) & (peers["min_fold24"] > 0.0)
            result.at[index, "neighborhood_positive_fraction"] = float(positive.mean())
            result.at[index, "neighborhood_size"] = int(len(peers))
    result["development_gate"] = (
        result["pre_neighborhood_gate"]
        & (result["neighborhood_size"] >= 6)
        & (result["neighborhood_positive_fraction"] >= 0.60)
    )
    return result


def reconstruct_selected(
    selected_row: pd.Series,
    boundaries: dict[str, pd.DataFrame],
    components: dict[str, dict[str, dict[int, np.ndarray]]],
    thresholds: dict[str, dict[tuple[str, int, float], np.ndarray]],
    paths: dict[str, SymbolPaths],
    trends: dict[str, dict[int, np.ndarray]],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    source = str(selected_row["source_symbol"])
    trade = str(selected_row["trade_symbol"])
    family = str(selected_row["family"])
    window = int(selected_row["window_seconds"])
    base_mask, side = candidate_mask_and_side(
        family=family,
        window=window,
        components=components[source],
        thresholds=thresholds[source],
        q_imbalance=float(selected_row["imbalance_quantile"]),
        q_volume=float(selected_row["volume_quantile"]),
    )
    mask = (
        base_mask
        & trend_mask(trends[trade], str(selected_row["trend_mode"]), side)
        & clock_mask(boundaries[source]["boundary_time_ms"].to_numpy(np.int64), str(selected_row["clock_mode"]))
    )
    hi = int(np.where(HORIZONS == int(selected_row["horizon_minutes"]))[0][0])
    si = int(np.where(STOPS == float(selected_row["stop_atr"]))[0][0])
    path = paths[trade]
    ledgers = {}
    selected_indices = None
    base_entry = None
    base_exit = None
    for cost in (18.0, 24.0, 32.0, 40.0):
        net, entry, exit_time, stopped = path.outcome(
            side=side,
            horizon_index=hi,
            stop_index=si,
            latency_index=0,
            round_trip_bp=cost,
        )
        chosen = greedy_one_slot(np.flatnonzero(mask & np.isfinite(net)), entry, exit_time)
        if selected_indices is None:
            selected_indices = chosen
            base_entry = entry
            base_exit = exit_time
        ledgers[cost] = (net, entry, exit_time, stopped, chosen)
    assert selected_indices is not None and base_entry is not None and base_exit is not None
    opposite_net, opposite_entry, opposite_exit, _ = path.outcome(
        side=-side,
        horizon_index=hi,
        stop_index=si,
        latency_index=0,
        round_trip_bp=24.0,
    )
    opposite_chosen = greedy_one_slot(
        np.flatnonzero(mask & np.isfinite(opposite_net)), opposite_entry, opposite_exit
    )

    rows = []
    for index in selected_indices:
        row = {
            "event_index": int(index),
            "event_time_ms": int(boundaries[source]["boundary_time_ms"].iloc[index]),
            "entry_time_ms": int(base_entry[index]),
            "exit_time_ms": int(base_exit[index]),
            "side": int(side[index]),
            "source_symbol": source,
            "trade_symbol": trade,
        }
        for cost, (net, _, _, stopped, _) in ledgers.items():
            row[f"net_log_{int(cost)}bp"] = float(net[index])
            row[f"stopped_{int(cost)}bp"] = int(stopped[index])
        rows.append(row)
    ledger = pd.DataFrame(rows)
    audit = {
        "candidate_id": str(selected_row["candidate_id"]),
        "cost_metrics": {
            str(int(cost)): metrics(
                values[0][values[4]],
                values[1][values[4]],
                fold_edges_ms=FOLD_EDGES_MS,
            )
            for cost, values in ledgers.items()
        },
        "opposite_direction_24bp": metrics(
            opposite_net[opposite_chosen],
            opposite_entry[opposite_chosen],
            fold_edges_ms=FOLD_EDGES_MS,
        ),
    }
    return ledger, audit


def main() -> int:
    args = arguments()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    registration = json.loads(args.registration.read_text(encoding="utf-8"))
    if registration["registration_id"] != "wave39-quarterhour-exact-aggtrades-v1":
        raise RuntimeError("registration identity mismatch")

    boundaries: dict[str, pd.DataFrame] = {}
    components: dict[str, dict[str, dict[int, np.ndarray]]] = {}
    thresholds: dict[str, dict[tuple[str, int, float], np.ndarray]] = {}
    paths: dict[str, SymbolPaths] = {}
    trends: dict[str, dict[int, np.ndarray]] = {}
    input_hashes: dict[str, str] = {}

    for symbol in SYMBOLS:
        boundary = load_boundary(args.data_root, symbol)
        contract, funding = load_support(args.data_root, symbol)
        symbol_paths, symbol_trends = build_paths(symbol, boundary, contract, funding)
        boundaries[symbol] = boundary
        components[symbol] = signal_components(boundary)
        thresholds[symbol] = build_thresholds(components[symbol])
        paths[symbol] = symbol_paths
        trends[symbol] = symbol_trends
        for path in (
            args.data_root / f"{symbol}_quarterhour_exact_2022.csv.gz",
            args.data_root / "support" / f"{symbol}_contract_1m_2022.csv.gz",
            args.data_root / "support" / f"{symbol}_funding_2022.csv.gz",
        ):
            input_hashes[str(path.relative_to(args.data_root))] = sha256_file(path)

    all_rows: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for family in (
            "WITHIN_ASSET_BOUNDARY_CONTINUATION",
            "WITHIN_ASSET_BOUNDARY_ABSORPTION",
        ):
            all_rows.extend(evaluate_family_grid(
                source_symbol=symbol,
                trade_symbol=symbol,
                family=family,
                windows=WINDOWS,
                boundary=boundaries[symbol],
                components=components[symbol],
                thresholds=thresholds[symbol],
                paths=paths[symbol],
                trade_trends=trends[symbol],
                reduced_cross_asset_grid=False,
            ))
        all_rows.extend(evaluate_family_grid(
            source_symbol=symbol,
            trade_symbol=symbol,
            family="BOUNDARY_FLOW_FLIP",
            windows=(60,),
            boundary=boundaries[symbol],
            components=components[symbol],
            thresholds=thresholds[symbol],
            paths=paths[symbol],
            trade_trends=trends[symbol],
            reduced_cross_asset_grid=False,
        ))

    for trade_symbol in ("ETHUSDT", "SOLUSDT", "XRPUSDT"):
        for family in (
            "WITHIN_ASSET_BOUNDARY_CONTINUATION",
            "WITHIN_ASSET_BOUNDARY_ABSORPTION",
        ):
            all_rows.extend(evaluate_family_grid(
                source_symbol="BTCUSDT",
                trade_symbol=trade_symbol,
                family=family,
                windows=WINDOWS,
                boundary=boundaries["BTCUSDT"],
                components=components["BTCUSDT"],
                thresholds=thresholds["BTCUSDT"],
                paths=paths[trade_symbol],
                trade_trends=trends[trade_symbol],
                reduced_cross_asset_grid=True,
            ))

    frame = pd.DataFrame(all_rows)
    if frame["candidate_id"].duplicated().any():
        duplicate = frame.loc[frame["candidate_id"].duplicated(), "candidate_id"].tolist()[:5]
        raise RuntimeError(f"candidate id collision: {duplicate}")
    frame = add_neighborhood_scores(frame)
    frame.sort_values(
        ["development_gate", "min_fold24", "net32", "top10_share24"],
        ascending=[False, False, False, True],
        inplace=True,
        kind="mergesort",
    )
    frame.to_csv(args.output_dir / "wave39_all_candidates_2022.csv.gz", index=False, compression="gzip")
    gated = frame.loc[frame["development_gate"]].copy()
    gated.to_csv(args.output_dir / "wave39_gated_candidates_2022.csv", index=False)

    selected_payload: dict[str, Any] | None = None
    if len(gated):
        selected = gated.iloc[0]
        ledger, selected_audit = reconstruct_selected(
            selected,
            boundaries,
            components,
            thresholds,
            paths,
            trends,
        )
        ledger.to_csv(args.output_dir / "wave39_selected_ledger_2022.csv", index=False)
        selected_payload = {
            "candidate": {
                key: (value.item() if isinstance(value, np.generic) else value)
                for key, value in selected.to_dict().items()
            },
            "audit": selected_audit,
        }
        (args.output_dir / "wave39_selected_candidate_2022.json").write_text(
            json.dumps(selected_payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )

    manifest = {
        "schema": "wave39-development-result-v1",
        "registration_sha256": sha256_file(args.registration),
        "input_hashes": input_hashes,
        "candidate_count": int(len(frame)),
        "pre_neighborhood_gate_count": int(frame["pre_neighborhood_gate"].sum()),
        "development_gate_count": int(frame["development_gate"].sum()),
        "selected_candidate_id": (
            str(selected_payload["candidate"]["candidate_id"]) if selected_payload else None
        ),
        "2023_opened": False,
        "2024_opened": False,
        "sealed_terminal_oos_opened": False,
        "risk_or_leverage_optimized": False,
        "results": {
            path.name: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for path in sorted(args.output_dir.glob("wave39_*"))
            if path.is_file()
        },
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

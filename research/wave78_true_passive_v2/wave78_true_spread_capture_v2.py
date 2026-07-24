#!/usr/bin/env python3
"""Wave78 V2: true passive round-trip spread capture with toxicity avoidance.

Research-only. Downloads are handled by the caller. This module consumes
checksum-verified Binance Vision USD-M daily bookTicker and aggTrades archives.
All features use completed one-second intervals. A single pending order or
position is allowed globally across BTCUSDT and ETHUSDT.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

import wave78_single_slot_passive as core

ROOT_URL = "https://data.binance.vision/data/futures/um/daily"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
TRAIN_DAYS = ("2023-06-27", "2023-07-15")
DEVCAL_DAYS = ("2023-08-30",)
SELECTION_DAYS = ("2023-09-15", "2023-10-25")
VALIDATION_DAYS = ("2023-11-15", "2023-12-28")
CONDITIONAL_TEST_DAYS = ("2024-02-15", "2024-05-15", "2024-08-15")
DECISION_CADENCE_SECONDS = 10
TTL_SECONDS = 5
HORIZON_SECONDS = 30
OWN_NOTIONAL = 1_000.0
OWN_DISPLAY_FRACTION = 0.01
EXIT_CAPACITY_FRACTION = 0.10
MAX_QUOTE_AGE_MS = 500
MAX_EMERGENCY_EXIT_DELAY_MS = 2_000
CANONICAL_LATENCY_MS = 100
STRESS_LATENCY_MS = 250
CANONICAL_QUEUE = 2.0
STRESS_QUEUE = 3.0
COSTS_BPS = (9.0, 13.0, 17.0)
FILL_THRESHOLDS = (0.05, 0.10, 0.20)
SAFE_THRESHOLDS = (0.55, 0.60, 0.65, 0.70)
MODEL_KINDS = ("logistic", "hist")
USER_AGENT = "smc-wave78-true-spread-v2/1.0"

DIRECTIONAL_FEATURES = (
    "l1_imbalance",
    "microprice_ticks",
    "flow_imb_1s",
    "flow_imb_5s",
    "flow_imb_30s",
    "flow_acceleration",
    "ret_1s_ticks",
    "ret_5s_ticks",
)
STATIC_FEATURES = (
    "is_eth",
    "spread_bps",
    "log_depth_notional",
    "quote_age_ms",
    "trade_notional_z",
    "trade_count_z",
    "rv_30s_ticks",
    "flow_price_efficiency",
)
MODEL_FEATURES = STATIC_FEATURES + tuple(f"side_{x}" for x in DIRECTIONAL_FEATURES)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def get(url: str, attempts: int = 7) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.read()
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"download failed {url}: {last!r}")


def verified_source(cache: Path, symbol: str, dtype: str, day: str) -> tuple[Path, dict]:
    cache.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}-{dtype}-{day}.zip"
    url = f"{ROOT_URL}/{dtype}/{symbol}/{name}"
    path = cache / name
    checksum_path = cache / f"{name}.CHECKSUM"
    if not path.exists():
        path.write_bytes(get(url))
    if not checksum_path.exists():
        checksum_path.write_bytes(get(url + ".CHECKSUM"))
    expected = checksum_path.read_text(encoding="utf-8-sig").strip().split()[0].lower()
    actual = sha256_file(path)
    if actual != expected:
        raise ValueError(f"checksum mismatch {name}: {actual} != {expected}")
    return path, {"url": url, "sha256": actual, "bytes": path.stat().st_size}


@dataclass(frozen=True)
class RoundTrip:
    signal_time_ms: int
    free_time_ms: int
    day: str
    symbol: str
    side: int
    score: float
    filled: bool
    fill_qty: float = 0.0
    entry_time_ms: int = -1
    entry_price: float = math.nan
    passive_exit_qty: float = 0.0
    passive_exit_price: float = math.nan
    passive_exit_time_ms: int = -1
    taker_exit_qty: float = 0.0
    taker_exit_price: float = math.nan
    taker_exit_time_ms: int = -1
    gross_log: float = math.nan
    exit_mode: str = "unfilled"
    emergency_delay_ms: int = 0


def side_frame(panel: pd.DataFrame, side: int) -> pd.DataFrame:
    if side not in (-1, 1):
        raise ValueError("side must be -1 or +1")
    frame = panel.copy()
    frame["side"] = side
    for feature in DIRECTIONAL_FEATURES:
        frame[f"side_{feature}"] = side * pd.to_numeric(frame[feature], errors="coerce")
    return frame


def decision_rows(panel: pd.DataFrame) -> pd.DataFrame:
    seconds = pd.to_numeric(panel.sec, errors="raise").astype(np.int64)
    valid = (
        (seconds % DECISION_CADENCE_SECONDS == 0)
        & panel[list(STATIC_FEATURES[:-1])].notna().all(axis=1)
        & panel[list(DIRECTIONAL_FEATURES)].notna().all(axis=1)
        & (panel.quote_age_ms <= MAX_QUOTE_AGE_MS)
        & (panel.spread_bps > 0)
        & (panel.spread_bps <= 5.0)
    )
    return panel.loc[valid].reset_index(drop=True)


def _entry_fill(
    day: core.MarketDay,
    row: pd.Series,
    side: int,
    *,
    latency_ms: int,
    queue_multiplier: float,
) -> tuple[int, float, float, int, int] | None:
    arrival = int(row.known_time_ms) + latency_ms
    book_times = day.book.event_time.to_numpy(np.int64)
    quote_index = core._last_quote_before(book_times, arrival, MAX_QUOTE_AGE_MS)
    if quote_index is None:
        return None
    if side > 0:
        price = float(day.book.best_bid_price.iloc[quote_index])
        shown = float(day.book.best_bid_qty.iloc[quote_index])
    else:
        price = float(day.book.best_ask_price.iloc[quote_index])
        shown = float(day.book.best_ask_qty.iloc[quote_index])
    if not (price > 0 and shown > 0):
        return None
    own_qty = min(OWN_NOTIONAL / price, shown * OWN_DISPLAY_FRACTION)
    if own_qty <= 0:
        return None
    queue_ahead = queue_multiplier * shown
    pending_end = arrival + TTL_SECONDS * 1_000
    trade_times = day.trades.time_ms.to_numpy(np.int64)
    begin = int(np.searchsorted(trade_times, arrival, side="left"))
    end = int(np.searchsorted(trade_times, pending_end, side="right"))
    consumed = 0.0
    for index in range(begin, end):
        buyer_maker = bool(day.trades.buyer_maker.iloc[index])
        opposing = buyer_maker if side > 0 else not buyer_maker
        trade_price = float(day.trades.price.iloc[index])
        through = trade_price <= price * (1 + 1e-12) if side > 0 else trade_price >= price * (1 - 1e-12)
        if not (opposing and through):
            continue
        consumed += float(day.trades.qty.iloc[index])
        if consumed > queue_ahead:
            fill_qty = min(own_qty, consumed - queue_ahead)
            if fill_qty > 0:
                return int(trade_times[index]), price, float(fill_qty), pending_end, arrival
    return None


def simulate_true_spread_roundtrip(
    day: core.MarketDay,
    row: pd.Series,
    side: int,
    score: float,
    *,
    latency_ms: int = CANONICAL_LATENCY_MS,
    queue_multiplier: float = CANONICAL_QUEUE,
) -> RoundTrip | None:
    signal_time = int(row.known_time_ms)
    entry = _entry_fill(day, row, side, latency_ms=latency_ms, queue_multiplier=queue_multiplier)
    if entry is None:
        return RoundTrip(
            signal_time_ms=signal_time,
            free_time_ms=signal_time + latency_ms + TTL_SECONDS * 1_000,
            day=day.day,
            symbol=day.symbol,
            side=side,
            score=score,
            filled=False,
        )
    entry_time, entry_price, fill_qty, _pending_end, _arrival = entry
    book_times = day.book.event_time.to_numpy(np.int64)
    exit_arrival = entry_time + latency_ms
    quote_index = core._last_quote_before(book_times, exit_arrival, MAX_QUOTE_AGE_MS)
    if quote_index is None:
        return None
    if side > 0:
        passive_exit_price = float(day.book.best_ask_price.iloc[quote_index])
        passive_shown = float(day.book.best_ask_qty.iloc[quote_index])
    else:
        passive_exit_price = float(day.book.best_bid_price.iloc[quote_index])
        passive_shown = float(day.book.best_bid_qty.iloc[quote_index])
    if not (passive_exit_price > 0 and passive_shown > 0):
        return None
    exit_queue = queue_multiplier * passive_shown
    timeout = entry_time + HORIZON_SECONDS * 1_000
    trade_times = day.trades.time_ms.to_numpy(np.int64)
    begin = int(np.searchsorted(trade_times, exit_arrival, side="left"))
    end = int(np.searchsorted(trade_times, timeout, side="right"))
    consumed = 0.0
    passive_qty = 0.0
    passive_time = -1
    for index in range(begin, end):
        buyer_maker = bool(day.trades.buyer_maker.iloc[index])
        opposing = (not buyer_maker) if side > 0 else buyer_maker
        trade_price = float(day.trades.price.iloc[index])
        through = trade_price >= passive_exit_price * (1 - 1e-12) if side > 0 else trade_price <= passive_exit_price * (1 + 1e-12)
        if not (opposing and through):
            continue
        consumed += float(day.trades.qty.iloc[index])
        new_qty = min(fill_qty, max(0.0, consumed - exit_queue))
        if new_qty > passive_qty:
            passive_qty = new_qty
            passive_time = int(trade_times[index])
        if passive_qty >= fill_qty - 1e-15:
            break

    remaining = max(0.0, fill_qty - passive_qty)
    taker_price = math.nan
    taker_time = -1
    emergency_delay = 0
    if remaining > 1e-15:
        emergency_order_time = timeout + latency_ms
        emergency_index = core._first_quote_after(book_times, emergency_order_time, MAX_EMERGENCY_EXIT_DELAY_MS)
        if emergency_index is None:
            return None
        taker_time = int(book_times[emergency_index])
        emergency_delay = taker_time - emergency_order_time
        if side > 0:
            taker_price = float(day.book.best_bid_price.iloc[emergency_index])
            taker_available = float(day.book.best_bid_qty.iloc[emergency_index])
        else:
            taker_price = float(day.book.best_ask_price.iloc[emergency_index])
            taker_available = float(day.book.best_ask_qty.iloc[emergency_index])
        if not (taker_price > 0 and taker_available > 0):
            return None
        if remaining > EXIT_CAPACITY_FRACTION * taker_available:
            return None

    weighted_log = 0.0
    if passive_qty > 0:
        weighted_log += passive_qty * side * math.log(passive_exit_price / entry_price)
    if remaining > 0:
        weighted_log += remaining * side * math.log(taker_price / entry_price)
    gross_log = weighted_log / fill_qty
    if remaining <= 1e-15:
        free_time = passive_time
        mode = "passive_passive"
    elif passive_qty > 0:
        free_time = taker_time
        mode = "passive_partial_then_taker"
    else:
        free_time = taker_time
        mode = "passive_entry_taker_exit"
    return RoundTrip(
        signal_time_ms=signal_time,
        free_time_ms=int(free_time),
        day=day.day,
        symbol=day.symbol,
        side=side,
        score=float(score),
        filled=True,
        fill_qty=float(fill_qty),
        entry_time_ms=int(entry_time),
        entry_price=float(entry_price),
        passive_exit_qty=float(passive_qty),
        passive_exit_price=float(passive_exit_price),
        passive_exit_time_ms=int(passive_time),
        taker_exit_qty=float(remaining),
        taker_exit_price=float(taker_price),
        taker_exit_time_ms=int(taker_time),
        gross_log=float(gross_log),
        exit_mode=mode,
        emergency_delay_ms=int(emergency_delay),
    )


def build_day(cache: Path, symbol: str, day: str, source_records: list[dict]) -> core.MarketDay:
    book_path, book_meta = verified_source(cache / symbol, symbol, "bookTicker", day)
    trade_path, trade_meta = verified_source(cache / symbol, symbol, "aggTrades", day)
    book = core.read_book(book_path)
    trades = core.read_trades(trade_path)
    panel = core.build_second_panel(symbol, day, book, trades)
    source_records.extend([
        {"symbol": symbol, "day": day, "type": "bookTicker", **book_meta},
        {"symbol": symbol, "day": day, "type": "aggTrades", **trade_meta},
    ])
    return core.MarketDay(symbol, day, panel, book, trades)


def model_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[list(MODEL_FEATURES)].replace([np.inf, -np.inf], np.nan)


def make_model(kind: str, *, random_state: int):
    if kind == "logistic":
        return make_pipeline(
            SimpleImputer(strategy="median", add_indicator=True),
            StandardScaler(),
            LogisticRegression(C=0.25, class_weight="balanced", max_iter=800, random_state=random_state),
        )
    if kind == "hist":
        return make_pipeline(
            SimpleImputer(strategy="median", add_indicator=True),
            HistGradientBoostingClassifier(
                learning_rate=0.05,
                max_iter=120,
                max_leaf_nodes=15,
                max_depth=4,
                min_samples_leaf=200,
                l2_regularization=8.0,
                random_state=random_state,
            ),
        )
    raise ValueError(kind)


def training_rows(days: Sequence[core.MarketDay]) -> pd.DataFrame:
    rows: list[dict] = []
    for day in days:
        candidate = decision_rows(day.panel)
        for side in (1, -1):
            features = side_frame(candidate, side)
            for _, row in features.iterrows():
                attempt = simulate_true_spread_roundtrip(day, row, side, score=0.0)
                if attempt is None:
                    continue
                record = {feature: row[feature] for feature in MODEL_FEATURES}
                record.update({
                    "day": day.day,
                    "symbol": day.symbol,
                    "signal_time_ms": attempt.signal_time_ms,
                    "side": side,
                    "filled": int(attempt.filled),
                    "gross_log": attempt.gross_log,
                    "safe_after_9bp": int(attempt.filled and attempt.gross_log - 9.0 / 10_000 > 0),
                    "exit_mode": attempt.exit_mode,
                })
                rows.append(record)
        print(json.dumps({"training_labels": len(rows), "symbol": day.symbol, "day": day.day}), flush=True)
    return pd.DataFrame(rows)


def fit_models(train: pd.DataFrame, kind: str):
    if train.empty or train.filled.nunique() < 2:
        raise ValueError("training fill labels are degenerate")
    fill_model = make_model(kind, random_state=7801)
    fill_model.fit(model_matrix(train), train.filled.astype(int))
    filled = train.filled.astype(bool)
    if int(filled.sum()) < 500 or train.loc[filled, "safe_after_9bp"].nunique() < 2:
        raise ValueError("insufficient filled safe/toxic labels")
    safe_model = make_model(kind, random_state=7802)
    sample_weight = None
    if kind == "hist":
        labels = train.loc[filled, "safe_after_9bp"].to_numpy(int)
        counts = np.bincount(labels, minlength=2)
        sample_weight = np.where(labels == 1, len(labels) / max(2 * counts[1], 1), len(labels) / max(2 * counts[0], 1))
    if sample_weight is None:
        safe_model.fit(model_matrix(train.loc[filled]), train.loc[filled, "safe_after_9bp"].astype(int))
    else:
        safe_model.fit(model_matrix(train.loc[filled]), train.loc[filled, "safe_after_9bp"].astype(int), histgradientboostingclassifier__sample_weight=sample_weight)
    return fill_model, safe_model


def scored_decisions(day: core.MarketDay, fill_model, safe_model, fill_min: float, safe_min: float) -> pd.DataFrame:
    candidate = decision_rows(day.panel)
    pieces: list[pd.DataFrame] = []
    for side in (1, -1):
        frame = side_frame(candidate, side)
        matrix = model_matrix(frame)
        result = frame[["sec", "known_time_ms", "symbol", "day", "spread_bps"]].copy()
        result["side"] = side
        result["fill_probability"] = fill_model.predict_proba(matrix)[:, 1]
        result["safe_probability"] = safe_model.predict_proba(matrix)[:, 1]
        result["edge_score"] = result.fill_probability * (result.safe_probability - 0.5) * result.spread_bps
        result = result[(result.fill_probability >= fill_min) & (result.safe_probability >= safe_min) & (result.edge_score > 0)]
        pieces.append(result)
    return pd.concat(pieces, ignore_index=True) if pieces else pd.DataFrame()


def choose_one_side(decisions: Iterable[pd.DataFrame]) -> pd.DataFrame:
    valid = [frame for frame in decisions if not frame.empty]
    if not valid:
        return pd.DataFrame()
    combined = pd.concat(valid, ignore_index=True)
    combined = combined.sort_values(
        ["known_time_ms", "edge_score", "symbol", "side"],
        ascending=[True, False, True, False],
        kind="mergesort",
    )
    chosen: list[pd.Series] = []
    for _, group in combined.groupby("known_time_ms", sort=True):
        first = group.iloc[0]
        if len(group) > 1 and math.isclose(float(first.edge_score), float(group.iloc[1].edge_score), rel_tol=0.0, abs_tol=1e-15):
            continue
        chosen.append(first)
    return pd.DataFrame(chosen).reset_index(drop=True) if chosen else pd.DataFrame()


def replay_candidate(
    days_by_key: dict[tuple[str, str], core.MarketDay],
    decisions: pd.DataFrame,
    *,
    latency_ms: int,
    queue_multiplier: float,
) -> pd.DataFrame:
    attempts: list[RoundTrip] = []
    if decisions.empty:
        return pd.DataFrame()
    for row in decisions.itertuples(index=False):
        day = days_by_key[(str(row.symbol), str(row.day))]
        panel = day.panel
        second = int(row.known_time_ms) // 1_000 - 1
        position = second - int(panel.sec.iloc[0])
        if position < 0 or position >= len(panel) or int(panel.known_time_ms.iloc[position]) != int(row.known_time_ms):
            continue
        feature_row = panel.iloc[position]
        attempt = simulate_true_spread_roundtrip(
            day,
            feature_row,
            int(row.side),
            float(row.edge_score),
            latency_ms=latency_ms,
            queue_multiplier=queue_multiplier,
        )
        if attempt is not None:
            attempts.append(attempt)
    return core.route_single_slot(attempts)


def metrics(routed: pd.DataFrame, cost_bps: float) -> dict:
    attempts = int(len(routed))
    if routed.empty:
        return {
            "attempts": 0, "fills": 0, "fill_rate": 0.0, "log_growth": 0.0,
            "profit_factor": 0.0, "max_drawdown": 0.0, "top20_positive_share": 1.0,
            "after_top20_positive": -1.0, "positive_day_fraction": 0.0,
            "min_day_fills": 0, "max_date_profit_share": 1.0,
            "max_symbol_fill_share": 1.0, "buy_fill_share": 0.0,
            "sell_fill_share": 0.0, "passive_passive_share": 0.0,
        }
    fills = routed[routed.filled.astype(bool)].copy()
    if fills.empty:
        output = metrics(pd.DataFrame(), cost_bps)
        output["attempts"] = attempts
        return output
    net_log = fills.gross_log.to_numpy(float) - cost_bps / 10_000
    simple = np.expm1(net_log)
    curve = np.exp(np.r_[0.0, np.cumsum(net_log)])
    peak = np.maximum.accumulate(curve)
    positive = simple[simple > 0]
    negative = -simple[simple < 0]
    positive_order = np.flatnonzero(simple > 0)[np.argsort(simple[simple > 0])[::-1]] if len(positive) else np.array([], dtype=int)
    removed = np.zeros(len(simple), dtype=bool)
    removed[positive_order[: min(20, len(positive_order))]] = True
    timestamps = pd.to_datetime(fills.fill_time_ms, unit="ms", utc=True)
    day_log = pd.Series(net_log).groupby(timestamps.dt.strftime("%Y-%m-%d")).sum()
    day_fills = pd.Series(1, index=np.arange(len(fills))).groupby(timestamps.dt.strftime("%Y-%m-%d")).sum()
    positive_days = day_log[day_log > 0]
    symbol_share = fills.symbol.value_counts(normalize=True)
    side_share = fills.side.value_counts(normalize=True)
    return {
        "attempts": attempts,
        "fills": int(len(fills)),
        "fill_rate": float(len(fills) / max(attempts, 1)),
        "log_growth": float(net_log.sum()),
        "profit_factor": float(positive.sum() / negative.sum()) if negative.sum() > 0 else (999.0 if positive.sum() > 0 else 0.0),
        "max_drawdown": float((1.0 - curve / peak).max()),
        "top20_positive_share": float(np.sort(positive)[-20:].sum() / positive.sum()) if positive.sum() > 0 else 1.0,
        "after_top20_positive": float(net_log[~removed].sum()),
        "positive_day_fraction": float((day_log > 0).mean()),
        "min_day_fills": int(day_fills.min()) if len(day_fills) else 0,
        "max_date_profit_share": float(positive_days.max() / positive_days.sum()) if positive_days.sum() > 0 else 1.0,
        "max_symbol_fill_share": float(symbol_share.max()) if len(symbol_share) else 1.0,
        "buy_fill_share": float(side_share.get(1, 0.0)),
        "sell_fill_share": float(side_share.get(-1, 0.0)),
        "passive_passive_share": float((fills.exit_mode == "passive_passive").mean()),
    }


def evaluate_block(days: Sequence[core.MarketDay], fill_model, safe_model, fill_min: float, safe_min: float) -> tuple[dict, dict[str, pd.DataFrame]]:
    days_by_key = {(day.symbol, day.day): day for day in days}
    decisions = choose_one_side([scored_decisions(day, fill_model, safe_model, fill_min, safe_min) for day in days])
    profiles = {
        "q2_l100": (CANONICAL_QUEUE, CANONICAL_LATENCY_MS),
        "q3_l100": (STRESS_QUEUE, CANONICAL_LATENCY_MS),
        "q2_l250": (CANONICAL_QUEUE, STRESS_LATENCY_MS),
    }
    records: dict = {"decision_rows": int(len(decisions))}
    ledgers: dict[str, pd.DataFrame] = {}
    for name, (queue, latency) in profiles.items():
        routed = replay_candidate(days_by_key, decisions, latency_ms=latency, queue_multiplier=queue)
        ledgers[name] = routed
        for cost in COSTS_BPS:
            for key, value in metrics(routed, cost).items():
                records[f"{name}_c{int(cost)}_{key}"] = value
    return records, ledgers


def devcal_gate(record: pd.Series) -> bool:
    return bool(
        record.q2_l100_c13_fills >= 300
        and record.q2_l100_c13_log_growth > 0
        and record.q2_l100_c17_log_growth > 0
        and record.q2_l100_c13_profit_factor >= 1.10
        and record.q2_l100_c13_top20_positive_share <= 0.35
        and record.q2_l100_c13_after_top20_positive > 0
        and record.q3_l100_c13_log_growth > 0
        and record.q2_l250_c13_log_growth > 0
        and record.q2_l100_c13_max_drawdown <= 0.10
        and record.q2_l100_c13_buy_fill_share >= 0.20
        and record.q2_l100_c13_sell_fill_share >= 0.20
        and record.q2_l100_c13_max_symbol_fill_share <= 0.70
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--open-test", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source_records: list[dict] = []
    loaded: dict[str, list[core.MarketDay]] = {}
    for stage, dates in (
        ("train", TRAIN_DAYS),
        ("devcal", DEVCAL_DAYS),
        ("selection", SELECTION_DAYS),
        ("validation", VALIDATION_DAYS),
    ):
        loaded[stage] = [build_day(args.cache, symbol, day, source_records) for day in dates for symbol in SYMBOLS]

    train = training_rows(loaded["train"])
    train.to_parquet(args.output / "training_labels.parquet", index=False, compression="zstd")
    rows: list[dict] = []
    fitted: dict[str, tuple[object, object]] = {}
    for kind in MODEL_KINDS:
        try:
            fill_model, safe_model = fit_models(train, kind)
        except Exception as exc:
            rows.append({"model_kind": kind, "model_error": repr(exc), "devcal_gate": False})
            continue
        fitted[kind] = (fill_model, safe_model)
        for fill_min in FILL_THRESHOLDS:
            for safe_min in SAFE_THRESHOLDS:
                record, _ = evaluate_block(loaded["devcal"], fill_model, safe_model, fill_min, safe_min)
                record.update({"model_kind": kind, "fill_min": fill_min, "safe_min": safe_min})
                rows.append(record)
    devcal = pd.DataFrame(rows)
    if not devcal.empty:
        devcal["devcal_gate"] = devcal.apply(lambda row: False if pd.notna(row.get("model_error")) else devcal_gate(row), axis=1)
        devcal["devcal_score"] = np.where(
            devcal.devcal_gate,
            np.minimum(devcal.q2_l100_c13_log_growth, devcal.q3_l100_c13_log_growth)
            + 0.5 * devcal.q2_l250_c13_log_growth
            - 0.25 * devcal.q2_l100_c13_max_drawdown,
            -1e9,
        )
        devcal = devcal.sort_values(["devcal_gate", "devcal_score", "model_kind", "fill_min", "safe_min"], ascending=[False, False, True, True, True], kind="mergesort")
    devcal.to_csv(args.output / "devcal_screen.csv", index=False)

    representatives = devcal[devcal.devcal_gate].head(12).copy() if not devcal.empty else pd.DataFrame()
    selection_rows: list[dict] = []
    for row in representatives.itertuples(index=False):
        fill_model, safe_model = fitted[str(row.model_kind)]
        record, _ = evaluate_block(loaded["selection"], fill_model, safe_model, float(row.fill_min), float(row.safe_min))
        candidate_id = f"{row.model_kind}|f{row.fill_min:.2f}|s{row.safe_min:.2f}"
        record.update({"candidate_id": candidate_id, "model_kind": row.model_kind, "fill_min": row.fill_min, "safe_min": row.safe_min})
        selection_rows.append(record)
    selection = pd.DataFrame(selection_rows)
    if not selection.empty:
        selection["selection_gate"] = (
            (selection.q2_l100_c13_fills >= 1000)
            & (selection.q2_l100_c13_min_day_fills >= 100)
            & (selection.q2_l100_c9_log_growth > 0)
            & (selection.q2_l100_c13_log_growth > 0)
            & (selection.q2_l100_c17_log_growth > 0)
            & (selection.q2_l100_c13_profit_factor >= 1.15)
            & (selection.q2_l100_c13_positive_day_fraction >= 0.60)
            & (selection.q2_l100_c13_top20_positive_share <= 0.25)
            & (selection.q2_l100_c13_after_top20_positive > 0)
            & (selection.q3_l100_c13_log_growth > 0)
            & (selection.q2_l250_c13_log_growth > 0)
            & (selection.q2_l100_c13_max_drawdown <= 0.10)
            & (selection.q2_l100_c13_max_symbol_fill_share <= 0.60)
            & (selection.q2_l100_c13_buy_fill_share >= 0.25)
            & (selection.q2_l100_c13_sell_fill_share >= 0.25)
            & (selection.q2_l100_c13_max_date_profit_share <= 0.35)
        )
        selection["selection_score"] = np.where(
            selection.selection_gate,
            np.minimum(selection.q2_l100_c13_log_growth, selection.q3_l100_c13_log_growth)
            + 0.5 * selection.q2_l250_c13_log_growth
            - 0.25 * selection.q2_l100_c13_max_drawdown,
            -1e9,
        )
        selection = selection.sort_values(["selection_gate", "selection_score", "candidate_id"], ascending=[False, False, True], kind="mergesort")
    selection.to_csv(args.output / "selection_screen.csv", index=False)

    primary = selection.iloc[0].to_dict() if len(selection) and bool(selection.iloc[0].selection_gate) else None
    validation_result = None
    if primary is not None:
        fill_model, safe_model = fitted[str(primary["model_kind"])]
        validation_result, validation_ledgers = evaluate_block(
            loaded["validation"], fill_model, safe_model, float(primary["fill_min"]), float(primary["safe_min"])
        )
        validation_result["validation_gate"] = bool(
            validation_result["q2_l100_c13_fills"] >= 1000
            and validation_result["q2_l100_c13_min_day_fills"] >= 100
            and validation_result["q2_l100_c9_log_growth"] > 0
            and validation_result["q2_l100_c13_log_growth"] > 0
            and validation_result["q2_l100_c17_log_growth"] > 0
            and validation_result["q2_l100_c13_profit_factor"] >= 1.15
            and validation_result["q2_l100_c13_positive_day_fraction"] >= 0.60
            and validation_result["q2_l100_c13_top20_positive_share"] <= 0.25
            and validation_result["q2_l100_c13_after_top20_positive"] > 0
            and validation_result["q3_l100_c13_log_growth"] > 0
            and validation_result["q2_l250_c13_log_growth"] > 0
            and validation_result["q2_l100_c13_max_drawdown"] <= 0.10
            and validation_result["q2_l100_c13_max_symbol_fill_share"] <= 0.60
            and validation_result["q2_l100_c13_buy_fill_share"] >= 0.25
            and validation_result["q2_l100_c13_sell_fill_share"] >= 0.25
            and validation_result["q2_l100_c13_max_date_profit_share"] <= 0.35
        )
        for name, ledger in validation_ledgers.items():
            ledger.to_csv(args.output / f"validation_{name}_ledger.csv", index=False)

    if args.open_test and validation_result and validation_result.get("validation_gate"):
        raise RuntimeError("conditional test opening is intentionally separated into a new immutable workflow")

    summary = {
        "study_id": "wave78_true_passive_roundtrip_v2",
        "train_days": TRAIN_DAYS,
        "devcal_days": DEVCAL_DAYS,
        "selection_days": SELECTION_DAYS,
        "validation_days": VALIDATION_DAYS,
        "conditional_test_days": CONDITIONAL_TEST_DAYS,
        "decision_cadence_seconds": DECISION_CADENCE_SECONDS,
        "ttl_seconds": TTL_SECONDS,
        "horizon_seconds": HORIZON_SECONDS,
        "training_rows": int(len(train)),
        "training_fills": int(train.filled.sum()) if len(train) else 0,
        "devcal_gate_count": int(devcal.devcal_gate.sum()) if len(devcal) else 0,
        "selection_gate_count": int(selection.selection_gate.sum()) if len(selection) else 0,
        "primary": primary,
        "validation": validation_result,
        "test_opened": false,
        "orders_submitted": false,
        "paper_or_live_started": false,
        "production_enabled": false,
        "source_records": source_records
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    hashes = []
    for path in sorted(args.output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            hashes.append(f"{sha256_file(path)}  {path.relative_to(args.output)}")
    (args.output / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(json.dumps({
        "training_rows": summary["training_rows"],
        "training_fills": summary["training_fills"],
        "devcal_gate_count": summary["devcal_gate_count"],
        "selection_gate_count": summary["selection_gate_count"],
        "primary": primary,
        "validation": validation_result,
        "test_opened": false
    }, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

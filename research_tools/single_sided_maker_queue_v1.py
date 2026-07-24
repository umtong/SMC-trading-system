#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT_URL = "https://data.binance.vision/data/futures/um/daily"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
TRAIN_DAY = "2023-01-03"
SELECTION_DAY = "2023-04-20"
VALIDATION_DAY = "2023-08-30"
TEST_DAY = "2023-12-28"
DEV_DAYS = (TRAIN_DAY, SELECTION_DAY, VALIDATION_DAY)
LATENCY_MS = 250
MAX_BBO_DELAY_MS = 2_000
RISK_FRACTION = 0.005
MAX_LEVERAGE = 5.0


@dataclass(frozen=True)
class Rule:
    name: str
    window: int
    flow_abs: float
    qz_min: float
    impact_max_bps: float
    book_mode: str


RULES = tuple(
    Rule(*x)
    for x in (
        ("abs_opp", 5, 0.40, 1.5, 1.0, "opp"),
        ("abs_opp_strict", 10, 0.55, 2.0, 0.5, "opp"),
        ("replenish", 5, 0.40, 1.5, 1.0, "replenish"),
        ("replenish_strict", 10, 0.55, 2.0, 0.5, "replenish"),
        ("micro_recover", 5, 0.50, 1.5, 1.0, "micro"),
        ("micro_recover_strict", 10, 0.65, 2.5, 0.5, "micro"),
    )
)
ORDER_LIVES = (1, 3, 5)
QUEUE_MULTIPLIERS = (1.0, 1.5, 2.0)
HOLD_SECONDS = (3, 10, 30)
STOP_SPREADS = (2.0, 4.0, 8.0)
EXIT_QUEUE_MULTIPLIERS = (1.0, 1.5)
COST_MULTIPLIERS = (1.0, 1.5, 2.0)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get(url: str, attempts: int = 6) -> bytes:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "smc-single-sided-maker-v1/1.0"})
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.read()
        except Exception as exc:
            error = exc
            if attempt + 1 < attempts:
                time.sleep(min(20, 2**attempt))
    raise RuntimeError(f"{url}: {error!r}")


def verified_archive(cache: Path, symbol: str, data_type: str, day: str) -> tuple[Path, dict]:
    cache.mkdir(parents=True, exist_ok=True)
    name = f"{symbol}-{data_type}-{day}.zip"
    path = cache / name
    checksum_path = cache / f"{name}.CHECKSUM"
    url = f"{ROOT_URL}/{data_type}/{symbol}/{name}"
    if not path.exists():
        path.write_bytes(get(url))
    if not checksum_path.exists():
        checksum_path.write_bytes(get(f"{url}.CHECKSUM"))
    expected = checksum_path.read_text(encoding="utf-8-sig").strip().split()[0].lower()
    observed = sha256_path(path)
    if observed != expected:
        raise ValueError(f"checksum mismatch: {name}: {observed} != {expected}")
    return path, {"url": url, "sha256": observed, "bytes": path.stat().st_size}


def normalize_ms(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.int64)
    return np.where(np.abs(values) >= 10**15, values // 1000, values).astype(np.int64)


def member_name(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        members = [x for x in archive.namelist() if x.lower().endswith(".csv")]
    if len(members) != 1:
        raise ValueError(f"expected one CSV in {path}, got {members}")
    return members[0]


def book_columns(path: Path) -> tuple[list[str], str]:
    with zipfile.ZipFile(path) as archive:
        name = member_name(path)
        probe = pd.read_csv(archive.open(name), nrows=2)
    columns = list(probe.columns)
    time_col = "transaction_time" if "transaction_time" in columns else "event_time"
    required = ["best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty", time_col]
    missing = [x for x in required if x not in columns]
    if missing:
        raise ValueError(f"bookTicker columns missing {missing}: {columns}")
    return required, time_col


def build_book_seconds(path: Path) -> pd.DataFrame:
    use, time_col = book_columns(path)
    pieces = []
    with zipfile.ZipFile(path) as archive:
        name = member_name(path)
        for chunk in pd.read_csv(archive.open(name), usecols=use, chunksize=750_000):
            for col in use:
                chunk[col] = pd.to_numeric(chunk[col], errors="raise")
            chunk[time_col] = normalize_ms(chunk[time_col].to_numpy(np.int64))
            chunk["sec"] = chunk[time_col].to_numpy(np.int64) // 1000
            count = chunk.groupby("sec", sort=False).size().rename("book_updates")
            last = chunk.sort_values(time_col, kind="mergesort").groupby("sec", sort=False).tail(1).set_index("sec")
            last = last[["best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty", time_col]].join(count)
            last = last.rename(columns={time_col: "last_book_ms"})
            pieces.append(last.reset_index())
    out = pd.concat(pieces, ignore_index=True)
    out = out.sort_values(["sec", "last_book_ms"], kind="mergesort").groupby("sec", sort=True).tail(1)
    # Update counts can span chunks. Recompute by summing chunk-level counts and retaining last BBO.
    counts = pd.concat([p[["sec", "book_updates"]] for p in pieces]).groupby("sec")["book_updates"].sum()
    out = out.drop(columns="book_updates").set_index("sec").join(counts).sort_index().reset_index()
    return out


def load_trade_arrays(path: Path) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    second_parts = []
    raw_t, raw_p, raw_q, raw_maker = [], [], [], []
    with zipfile.ZipFile(path) as archive:
        name = member_name(path)
        use = ["price", "quantity", "transact_time", "is_buyer_maker"]
        for chunk in pd.read_csv(archive.open(name), usecols=use, chunksize=1_000_000):
            price = pd.to_numeric(chunk.price, errors="raise").to_numpy(np.float64)
            quantity = pd.to_numeric(chunk.quantity, errors="raise").to_numpy(np.float64)
            tm = normalize_ms(pd.to_numeric(chunk.transact_time, errors="raise").to_numpy(np.int64))
            maker = chunk.is_buyer_maker.astype(str).str.lower().isin(["true", "1"]).to_numpy(bool)
            quote = price * quantity
            signed = np.where(maker, -quote, quote)
            sec = tm // 1000
            raw_t.append(tm); raw_p.append(price); raw_q.append(quantity); raw_maker.append(maker)
            frame = pd.DataFrame({
                "sec": sec, "quote": quote, "signed": signed,
                "buyer_qty": np.where(maker, 0.0, quantity),
                "seller_qty": np.where(maker, quantity, 0.0),
                "trade_count": 1,
                "trade_open": price,
                "trade_close": price,
                "trade_low": price,
                "trade_high": price,
            })
            second_parts.append(frame.groupby("sec", sort=False).agg({
                "quote": "sum", "signed": "sum", "buyer_qty": "sum", "seller_qty": "sum",
                "trade_count": "sum", "trade_open": "first", "trade_close": "last",
                "trade_low": "min", "trade_high": "max",
            }).reset_index())
    seconds = pd.concat(second_parts, ignore_index=True).groupby("sec", sort=True).agg({
        "quote": "sum", "signed": "sum", "buyer_qty": "sum", "seller_qty": "sum",
        "trade_count": "sum", "trade_open": "first", "trade_close": "last",
        "trade_low": "min", "trade_high": "max",
    }).reset_index()
    arrays = {
        "time": np.concatenate(raw_t), "price": np.concatenate(raw_p),
        "qty": np.concatenate(raw_q), "buyer_maker": np.concatenate(raw_maker),
    }
    order = np.argsort(arrays["time"], kind="mergesort")
    arrays = {k: v[order] for k, v in arrays.items()}
    return seconds, arrays


def trailing_z(series: pd.Series, window: int = 3600, minimum: int = 1200) -> pd.Series:
    roll = series.rolling(window, min_periods=minimum)
    return (series - roll.mean().shift(1)) / roll.std(ddof=0).shift(1).replace(0, np.nan)


def build_second_panel(symbol: str, day: str, book_path: Path, trade_path: Path) -> tuple[pd.DataFrame, dict[str, np.ndarray]]:
    book = build_book_seconds(book_path)
    trade, arrays = load_trade_arrays(trade_path)
    start = int(pd.Timestamp(day, tz="UTC").timestamp())
    index = np.arange(start, start + 86_400, dtype=np.int64)
    panel = pd.DataFrame({"sec": index}).merge(book, on="sec", how="left").merge(trade, on="sec", how="left")
    for col in ["best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty", "last_book_ms", "book_updates"]:
        panel[col] = panel[col].ffill()
    for col in ["quote", "signed", "buyer_qty", "seller_qty", "trade_count"]:
        panel[col] = panel[col].fillna(0.0)
    panel = panel.dropna(subset=["best_bid_price", "best_ask_price"]).copy()
    bid = panel.best_bid_price; ask = panel.best_ask_price
    bq = panel.best_bid_qty; aq = panel.best_ask_qty
    mid = (bid + ask) / 2.0; depth = bq + aq
    panel["symbol"] = symbol; panel["day"] = day
    panel["mid"] = mid; panel["spread"] = ask - bid; panel["spread_rel"] = panel.spread / mid
    panel["l1_imb"] = (bq - aq) / depth.replace(0, np.nan)
    panel["micro_dev"] = ((ask * bq + bid * aq) / depth.replace(0, np.nan) - mid) / mid
    panel["bid_delta_1"] = bq - bq.shift(1); panel["ask_delta_1"] = aq - aq.shift(1)
    for window in sorted({r.window for r in RULES}):
        quote = panel.quote.rolling(window, min_periods=window).sum()
        signed = panel.signed.rolling(window, min_periods=window).sum()
        panel[f"flow_{window}"] = signed / quote.replace(0, np.nan)
        panel[f"ret_{window}_bps"] = np.log(mid / mid.shift(window)) * 10_000.0
        panel[f"qz_{window}"] = trailing_z(np.log1p(quote))
        panel[f"bid_delta_{window}"] = bq - bq.shift(window)
        panel[f"ask_delta_{window}"] = aq - aq.shift(window)
        panel[f"micro_delta_{window}"] = panel.micro_dev - panel.micro_dev.shift(window)
    panel["known_ms"] = (panel.sec.to_numpy(np.int64) + 1) * 1000
    return panel, arrays


def resolve_first_bbo(path: Path, targets: np.ndarray) -> pd.DataFrame:
    use, time_col = book_columns(path)
    order = np.argsort(targets, kind="mergesort")
    sorted_targets = targets[order]
    result_time = np.full(len(targets), -1, dtype=np.int64)
    result = {x: np.full(len(targets), np.nan, dtype=float) for x in use if x != time_col}
    cursor = 0
    with zipfile.ZipFile(path) as archive:
        name = member_name(path)
        for chunk in pd.read_csv(archive.open(name), usecols=use, chunksize=750_000):
            for col in use:
                chunk[col] = pd.to_numeric(chunk[col], errors="raise")
            times = normalize_ms(chunk[time_col].to_numpy(np.int64))
            if len(times) == 0:
                continue
            # Resolve every target whose first eligible update is contained in this chunk.
            while cursor < len(sorted_targets) and sorted_targets[cursor] <= times[-1]:
                target = sorted_targets[cursor]
                pos = int(np.searchsorted(times, target, side="left"))
                original = order[cursor]
                if pos < len(times) and times[pos] - target <= MAX_BBO_DELAY_MS:
                    result_time[original] = times[pos]
                    for col in result:
                        result[col][original] = float(chunk[col].iloc[pos])
                cursor += 1
            if cursor >= len(sorted_targets):
                break
    frame = pd.DataFrame({"submit_ms": targets, "bbo_ms": result_time, **result})
    return frame


def candidate_frame(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for rule in RULES:
        flow = panel[f"flow_{rule.window}"]
        side = -np.sign(flow).astype(np.int8)
        impact = np.sign(flow) * panel[f"ret_{rule.window}_bps"]
        mask = flow.abs().ge(rule.flow_abs) & panel[f"qz_{rule.window}"].ge(rule.qz_min) & impact.le(rule.impact_max_bps) & side.ne(0)
        if rule.book_mode == "opp":
            mask &= (np.sign(flow) * panel.l1_imb).le(-0.10)
        elif rule.book_mode == "replenish":
            replenish = np.where(flow < 0, panel[f"bid_delta_{rule.window}"], panel[f"ask_delta_{rule.window}"])
            mask &= pd.Series(replenish, index=panel.index).gt(0)
        elif rule.book_mode == "micro":
            mask &= (np.sign(flow) * panel.micro_dev).le(0) & (np.sign(flow) * panel[f"micro_delta_{rule.window}"]).lt(0)
        q = panel.loc[mask, ["symbol", "day", "sec", "known_ms", "spread", "mid", "best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty", "l1_imb", f"flow_{rule.window}", f"qz_{rule.window}", f"ret_{rule.window}_bps"]].copy()
        q["side"] = side[mask].to_numpy(np.int8)
        q["rule"] = rule.name
        q["score"] = q[f"qz_{rule.window}"] + q[f"flow_{rule.window}"].abs() + np.maximum(rule.impact_max_bps - impact[mask], 0) * 0.1
        rows.append(q)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    # Identical second/rule-side duplicates are unnecessary; strongest state wins.
    out = out.sort_values(["known_ms", "score", "symbol"], ascending=[True, False, True], kind="mergesort")
    return out.drop_duplicates(["known_ms", "symbol", "side", "rule"], keep="first").reset_index(drop=True)


def entry_fill(candidate, bbo, trades, life_s: int, queue_mult: float, panel_by_sec: pd.DataFrame):
    if int(bbo.bbo_ms) < 0:
        return None
    side = int(candidate.side)
    price = float(bbo.best_bid_price if side > 0 else bbo.best_ask_price)
    displayed = float(bbo.best_bid_qty if side > 0 else bbo.best_ask_qty)
    queue = max(displayed * queue_mult, 0.0)
    start = int(bbo.bbo_ms); end = start + life_s * 1000
    t = trades["time"]; lo = int(np.searchsorted(t, start, side="left")); hi = int(np.searchsorted(t, end, side="right"))
    cumulative = 0.0
    for j in range(lo, hi):
        tm = int(t[j]); sec = tm // 1000
        if sec in panel_by_sec.index:
            state = panel_by_sec.loc[sec]
            current_best = float(state.best_bid_price if side > 0 else state.best_ask_price)
            # Top-of-book-only contract: cancel when the quote level is no longer best before fill.
            if not math.isclose(current_best, price, rel_tol=0, abs_tol=max(price * 1e-10, 1e-12)):
                return None
        maker = bool(trades["buyer_maker"][j]); trade_price = float(trades["price"][j])
        eligible = (side > 0 and maker and trade_price <= price) or (side < 0 and (not maker) and trade_price >= price)
        if eligible:
            cumulative += float(trades["qty"][j])
            if cumulative >= queue:
                return {"fill_ms": tm, "entry_price": price, "entry_queue": queue}
    return None


def exit_outcome(candidate, fill, trades, hold_s: int, stop_spreads: float, exit_queue_mult: float, panel_by_sec: pd.DataFrame):
    side = int(candidate.side); fill_ms = int(fill["fill_ms"]); entry = float(fill["entry_price"])
    fill_sec = fill_ms // 1000
    if fill_sec not in panel_by_sec.index:
        return None
    state = panel_by_sec.loc[fill_sec]
    exit_price = float(state.best_ask_price if side > 0 else state.best_bid_price)
    exit_queue = float(state.best_ask_qty if side > 0 else state.best_bid_qty) * exit_queue_mult
    spread = max(float(state.best_ask_price - state.best_bid_price), entry * 1e-8)
    stop_distance = stop_spreads * spread
    deadline = fill_ms + hold_s * 1000
    t = trades["time"]; lo = int(np.searchsorted(t, fill_ms, side="right")); hi = int(np.searchsorted(t, deadline, side="right"))
    queue_done = 0.0; maker_exit = None
    stop_exit = None
    last_checked_sec = fill_sec - 1
    for j in range(lo, hi):
        tm = int(t[j]); sec = tm // 1000
        if sec != last_checked_sec and sec in panel_by_sec.index:
            st = panel_by_sec.loc[sec]
            if side > 0 and float(st.best_bid_price) <= entry - stop_distance:
                stop_exit = (tm, float(st.best_bid_price)); break
            if side < 0 and float(st.best_ask_price) >= entry + stop_distance:
                stop_exit = (tm, float(st.best_ask_price)); break
            last_checked_sec = sec
        maker = bool(trades["buyer_maker"][j]); px = float(trades["price"][j])
        eligible = (side > 0 and (not maker) and px >= exit_price) or (side < 0 and maker and px <= exit_price)
        if eligible:
            queue_done += float(trades["qty"][j])
            if queue_done >= exit_queue:
                maker_exit = (tm, exit_price); break
    if stop_exit is not None:
        tm, px = stop_exit; kind = "taker_stop"
    elif maker_exit is not None:
        tm, px = maker_exit; kind = "maker_exit"
    else:
        sec = deadline // 1000
        eligible_secs = panel_by_sec.index[panel_by_sec.index >= sec]
        if len(eligible_secs) == 0:
            return None
        st = panel_by_sec.loc[int(eligible_secs[0])]
        tm = int(eligible_secs[0]) * 1000
        px = float(st.best_bid_price if side > 0 else st.best_ask_price)
        kind = "taker_horizon"
    gross_simple = side * (px / entry - 1.0)
    return {"exit_ms": int(tm), "exit_price": float(px), "exit_kind": kind, "gross_simple": float(gross_simple), "stop_distance": float(stop_distance)}


def costed_account_log(gross_simple: float, entry: float, stop_distance: float, exit_kind: str, multiplier: float) -> tuple[float, float, float]:
    maker_fee = 0.00020 * multiplier
    taker_fee = 0.00055 * multiplier
    taker_slip = 0.00005 * multiplier
    entry_cost = maker_fee
    exit_cost = maker_fee if exit_kind == "maker_exit" else taker_fee + taker_slip
    unit_stop = stop_distance / entry + entry_cost + taker_fee + taker_slip
    leverage = min(MAX_LEVERAGE, RISK_FRACTION / max(unit_stop, 1e-8))
    account_simple = leverage * (gross_simple - entry_cost - exit_cost)
    account_simple = max(account_simple, -0.999999)
    return float(math.log1p(account_simple)), float(leverage), float(unit_stop)


def build_outcomes(symbol: str, day: str, cache: Path, output: Path) -> dict:
    book_path, book_meta = verified_archive(cache, symbol, "bookTicker", day)
    trade_path, trade_meta = verified_archive(cache, symbol, "aggTrades", day)
    panel, trades = build_second_panel(symbol, day, book_path, trade_path)
    candidates = candidate_frame(panel)
    if candidates.empty:
        raise RuntimeError(f"no candidates: {symbol} {day}")
    targets = candidates.known_ms.to_numpy(np.int64) + LATENCY_MS
    bbo = resolve_first_bbo(book_path, targets)
    candidates = pd.concat([candidates.reset_index(drop=True), bbo.drop(columns="submit_ms")], axis=1)
    panel_by_sec = panel.set_index("sec", drop=False)
    rows = []
    for candidate in candidates.itertuples(index=False):
        for life, queue_mult in product(ORDER_LIVES, QUEUE_MULTIPLIERS):
            fill = entry_fill(candidate, candidate, trades, life, queue_mult, panel_by_sec)
            if fill is None:
                continue
            for hold, stop_spreads, exit_queue_mult in product(HOLD_SECONDS, STOP_SPREADS, EXIT_QUEUE_MULTIPLIERS):
                outcome = exit_outcome(candidate, fill, trades, hold, stop_spreads, exit_queue_mult, panel_by_sec)
                if outcome is None:
                    continue
                rec = {
                    "symbol": symbol, "day": day, "decision_ms": int(candidate.known_ms),
                    "entry_ms": int(fill["fill_ms"]), "exit_ms": int(outcome["exit_ms"]),
                    "side": int(candidate.side), "rule": str(candidate.rule), "score": float(candidate.score),
                    "life_s": life, "queue_mult": queue_mult, "hold_s": hold,
                    "stop_spreads": stop_spreads, "exit_queue_mult": exit_queue_mult,
                    "entry_price": fill["entry_price"], "exit_price": outcome["exit_price"],
                    "exit_kind": outcome["exit_kind"], "gross_simple": outcome["gross_simple"],
                    "entry_queue": fill["entry_queue"], "stop_distance": outcome["stop_distance"],
                }
                rec["policy_id"] = f"{rec['rule']}|l{life}|q{queue_mult}|h{hold}|s{stop_spreads}|xq{exit_queue_mult}"
                for multiplier in COST_MULTIPLIERS:
                    logret, lev, unit = costed_account_log(rec["gross_simple"], rec["entry_price"], rec["stop_distance"], rec["exit_kind"], multiplier)
                    tag = str(multiplier).replace(".", "p")
                    rec[f"account_log_{tag}x"] = logret; rec[f"leverage_{tag}x"] = lev; rec[f"unit_stop_{tag}x"] = unit
                rows.append(rec)
    result = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{symbol}_{day}_maker_outcomes.csv.gz"
    result.to_csv(path, index=False, compression={"method": "gzip", "compresslevel": 6, "mtime": 0})
    manifest = {
        "contract": "SINGLE_SIDED_MAKER_QUEUE_V1", "symbol": symbol, "day": day,
        "second_rows": len(panel), "raw_candidates": len(candidates), "outcomes": len(result),
        "book_source": book_meta, "trade_source": trade_meta, "output_sha256": sha256_path(path),
        "latency_ms": LATENCY_MS, "risk_fraction": RISK_FRACTION, "max_leverage": MAX_LEVERAGE,
        "rules": [r.__dict__ for r in RULES], "orders_submitted": False, "credentials_used": False,
    }
    (output / f"{symbol}_{day}_manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def route_global(frame: pd.DataFrame, log_col: str) -> pd.DataFrame:
    if frame.empty:
        return frame
    q = frame.sort_values(["entry_ms", "score", "symbol"], ascending=[True, False, True], kind="mergesort")
    rows = []; free = -1
    for entry_ms, group in q.groupby("entry_ms", sort=True):
        entry_ms = int(entry_ms)
        if entry_ms < free:
            continue
        row = group.iloc[int(np.argmax(group.score.to_numpy(float)))]
        rows.append(row)
        free = int(row.exit_ms) + 1
    return pd.DataFrame(rows).reset_index(drop=True)


def metrics(frame: pd.DataFrame, col: str) -> dict:
    if frame.empty:
        return {"trades": 0, "growth": 0.0, "mean_log": None, "pf": 0.0, "mdd": 0.0, "top5_share": 1.0, "win_rate": 0.0}
    logret = pd.to_numeric(frame[col], errors="coerce").dropna().to_numpy(float)
    simple = np.expm1(logret)
    equity = np.exp(np.r_[0.0, np.cumsum(logret)])
    dd = equity / np.maximum.accumulate(equity) - 1.0
    pos = simple[simple > 0]; neg = -simple[simple < 0]
    return {
        "trades": int(len(logret)), "growth": float(math.exp(logret.sum()) - 1.0),
        "mean_log": float(logret.mean()), "pf": float(pos.sum() / neg.sum()) if neg.sum() else (999.0 if pos.sum() else 0.0),
        "mdd": float(dd.min()), "top5_share": float(np.sort(pos)[-5:].sum() / pos.sum()) if pos.sum() else 1.0,
        "win_rate": float((simple > 0).mean()),
    }


def evaluate(input_dir: Path, output: Path) -> dict:
    files = sorted(input_dir.rglob("*_maker_outcomes.csv.gz"))
    if not files:
        raise FileNotFoundError(input_dir)
    data = pd.concat([pd.read_csv(path) for path in files], ignore_index=True)
    policies = sorted(data.policy_id.unique())
    rows = []
    for policy in policies:
        rec = {"policy_id": policy}
        for day, tag in ((TRAIN_DAY, "train"), (SELECTION_DAY, "selection"), (VALIDATION_DAY, "validation")):
            day_frame = data[(data.policy_id == policy) & (data.day == day)]
            for multiplier in COST_MULTIPLIERS:
                mtag = str(multiplier).replace(".", "p")
                log_col = f"account_log_{mtag}x"
                ledger = route_global(day_frame, log_col)
                measure = metrics(ledger, log_col)
                for key, value in measure.items():
                    rec[f"{tag}_{mtag}x_{key}"] = value
        # Selection never sees validation when ranking. Validation is a fixed pass/fail gate only.
        rec["dev_eligible"] = all(
            rec[f"{tag}_{mtag}x_trades"] >= 100
            and rec[f"{tag}_{mtag}x_growth"] > 0
            and rec[f"{tag}_{mtag}x_pf"] >= 1.05
            and rec[f"{tag}_{mtag}x_top5_share"] <= 0.20
            for tag in ("train", "selection") for mtag in ("1p0", "1p5")
        )
        rec["dev_score"] = min(rec["train_1p5x_growth"], rec["selection_1p5x_growth"]) if rec["dev_eligible"] else -1e9
        rec["validation_pass"] = bool(
            rec["dev_eligible"]
            and rec["validation_1p0x_trades"] >= 100
            and rec["validation_1p5x_trades"] >= 100
            and rec["validation_1p0x_growth"] > 0
            and rec["validation_1p5x_growth"] > 0
            and rec["validation_1p0x_pf"] >= 1.05
            and rec["validation_1p0x_top5_share"] <= 0.20
        )
        rows.append(rec)
    screen = pd.DataFrame(rows).sort_values(["dev_score", "selection_1p5x_growth", "policy_id"], ascending=[False, False, True], kind="mergesort")
    output.mkdir(parents=True, exist_ok=True)
    screen.to_csv(output / "screen.csv", index=False)
    survivors = screen[screen.validation_pass].copy()
    survivors.to_csv(output / "validation_survivors.csv", index=False)
    selected = survivors.head(1)
    summary = {
        "status": "COMPLETE", "screened": len(screen), "development_eligible": int(screen.dev_eligible.sum()),
        "validation_survivors": len(survivors), "test_authorized": bool(len(selected)),
        "selected_policy": selected.iloc[0].replace({np.nan: None}).to_dict() if len(selected) else None,
        "same_signals_across_costs": True, "paper_or_live_authority": False, "orders_submitted": False,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def test_selected(input_dir: Path, selected_path: Path, output: Path) -> dict:
    selection = json.loads(selected_path.read_text())
    policy = selection.get("selected_policy", {}).get("policy_id") if selection.get("selected_policy") else None
    if not policy or not selection.get("test_authorized"):
        raise RuntimeError("test is not authorized")
    files = sorted(input_dir.rglob(f"*_{TEST_DAY}_maker_outcomes.csv.gz"))
    data = pd.concat([pd.read_csv(path) for path in files], ignore_index=True)
    q = data[data.policy_id == policy]
    result = {"policy_id": policy, "day": TEST_DAY}
    ledgers = []
    for multiplier in COST_MULTIPLIERS:
        tag = str(multiplier).replace(".", "p")
        col = f"account_log_{tag}x"
        ledger = route_global(q, col)
        ledger["cost_multiplier"] = multiplier
        ledgers.append(ledger)
        result[f"{tag}x"] = metrics(ledger, col)
    output.mkdir(parents=True, exist_ok=True)
    pd.concat(ledgers, ignore_index=True).to_csv(output / "test_ledger.csv", index=False)
    result["strict_target_gate_passed"] = bool(
        result["1p0x"]["trades"] >= 100 and result["1p0x"]["growth"] >= 0.01
        and result["1p5x"]["growth"] > 0 and result["1p0x"]["top5_share"] <= 0.20
    )
    result["promotion_allowed"] = False
    (output / "test_summary.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--symbol", choices=SYMBOLS, required=True)
    b.add_argument("--days", nargs="+", required=True)
    b.add_argument("--cache", type=Path, required=True)
    b.add_argument("--output", type=Path, required=True)
    e = sub.add_parser("evaluate")
    e.add_argument("--input", type=Path, required=True)
    e.add_argument("--output", type=Path, required=True)
    t = sub.add_parser("test")
    t.add_argument("--input", type=Path, required=True)
    t.add_argument("--selected", type=Path, required=True)
    t.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "build":
        manifests = [build_outcomes(args.symbol, day, args.cache, args.output) for day in args.days]
        print(json.dumps(manifests, indent=2))
    elif args.command == "evaluate":
        print(json.dumps(evaluate(args.input, args.output), indent=2, default=str))
    else:
        print(json.dumps(test_selected(args.input, args.selected, args.output), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

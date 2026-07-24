from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from huggingface_hub import snapshot_download

REPO_ID = "Mindbyte-89/btcusdt-microbar-v2"
DATASET_REVISION = "1d41abbecffb7a098a8faf7d86b6481a091d6561"
STREAMS = ("trades", "book_ticks", "depth", "liquidations", "mark_price")
BASE_EXTRA_COST_BPS = 10.0
STRESS_COST_BPS = 15.0
EXTREME_COST_BPS = 20.0
MAX_EXEC_DELAY_MS = 2_000


@dataclass(frozen=True)
class Policy:
    family: str
    liq_z: float
    confirm_s: int
    depth_ratio: float
    flow_z: float
    reclaim_bps: float
    horizon_s: int

    @property
    def policy_id(self) -> str:
        return (
            f"{self.family}|lz{self.liq_z:g}|c{self.confirm_s}|"
            f"dr{self.depth_ratio:g}|fz{self.flow_z:g}|"
            f"rb{self.reclaim_bps:g}|h{self.horizon_s}"
        )


def trimmed_mean(values: np.ndarray, n: int = 5) -> float:
    values = values[np.isfinite(values)]
    if values.size <= n:
        return math.nan
    return float(np.sort(values)[:-n].mean())


def prior_z(x: pd.Series, window: int = 1800, minp: int = 300) -> pd.Series:
    past = x.shift(1)
    mean = past.rolling(window, min_periods=minp).mean()
    std = past.rolling(window, min_periods=minp).std(ddof=0).replace(0, np.nan)
    return (x - mean) / std


def read_many(paths: Iterable[Path], columns: list[str] | None = None) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted(paths):
        try:
            frames.append(pd.read_parquet(path, columns=columns))
        except Exception:
            frames.append(pd.read_parquet(path))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def dates_under(root: Path, stream: str) -> set[str]:
    base = root / stream
    if not base.exists():
        return set()
    return {p.name for p in base.iterdir() if p.is_dir() and len(p.name) == 10}


def aggregate_day(root: Path, day: str) -> pd.DataFrame:
    trades = read_many((root / "trades" / day).glob("*.parquet"))
    books = read_many((root / "book_ticks" / day).glob("*.parquet"))
    depth = read_many((root / "depth" / day).glob("*.parquet"))
    liquid = read_many((root / "liquidations" / day).glob("*.parquet"))
    mark = read_many((root / "mark_price" / day).glob("*.parquet"))
    if books.empty or depth.empty:
        return pd.DataFrame()

    for frame in (trades, books, depth, liquid, mark):
        if frame.empty:
            continue
        frame["timestamp_ms"] = pd.to_numeric(frame["timestamp_ms"], errors="coerce")
        frame.dropna(subset=["timestamp_ms"], inplace=True)
        frame["timestamp_ms"] = frame["timestamp_ms"].astype(np.int64)
        frame["sec"] = frame["timestamp_ms"] // 1000

    books = books.sort_values("timestamp_ms", kind="mergesort")
    last_book = books.groupby("sec", sort=True).tail(1).set_index("sec")
    last_book = last_book[["bid_price", "bid_qty", "ask_price", "ask_qty", "timestamp_ms"]].rename(
        columns={"timestamp_ms": "last_book_time_ms"}
    )
    first_book = books.groupby("sec", sort=True).head(1).set_index("sec")
    first_book = first_book[["bid_price", "ask_price", "timestamp_ms"]].rename(
        columns={
            "bid_price": "first_bid_price",
            "ask_price": "first_ask_price",
            "timestamp_ms": "first_book_time_ms",
        }
    )

    depth = depth.sort_values("timestamp_ms", kind="mergesort")
    last_depth = depth.groupby("sec", sort=True).tail(1).set_index("sec")
    bid_qty_cols = [f"bid_qty_{i}" for i in range(5) if f"bid_qty_{i}" in last_depth]
    ask_qty_cols = [f"ask_qty_{i}" for i in range(5) if f"ask_qty_{i}" in last_depth]
    bid_price_cols = [f"bid_price_{i}" for i in range(5) if f"bid_price_{i}" in last_depth]
    ask_price_cols = [f"ask_price_{i}" for i in range(5) if f"ask_price_{i}" in last_depth]
    depth_state = pd.DataFrame(index=last_depth.index)
    depth_state["bid_depth_quote"] = sum(
        pd.to_numeric(last_depth[q], errors="coerce")
        * pd.to_numeric(last_depth[p], errors="coerce")
        for q, p in zip(bid_qty_cols, bid_price_cols)
    )
    depth_state["ask_depth_quote"] = sum(
        pd.to_numeric(last_depth[q], errors="coerce")
        * pd.to_numeric(last_depth[p], errors="coerce")
        for q, p in zip(ask_qty_cols, ask_price_cols)
    )

    start = max(int(last_book.index.min()), int(depth_state.index.min()))
    end = min(int(last_book.index.max()), int(depth_state.index.max()))
    index = pd.Index(np.arange(start, end + 1, dtype=np.int64), name="sec")

    # State features use only the last quote/depth update in a completed second.
    state = pd.DataFrame(index=index).join(last_book).join(depth_state)
    quote_seen = pd.Series(last_book.index, index=last_book.index).reindex(index).ffill()
    depth_seen = pd.Series(depth_state.index, index=depth_state.index).reindex(index).ffill()
    state = state.ffill()
    state.loc[
        (index.to_numpy() - quote_seen.to_numpy()) > 2,
        ["bid_price", "bid_qty", "ask_price", "ask_qty", "last_book_time_ms"],
    ] = np.nan
    state.loc[
        (index.to_numpy() - depth_seen.to_numpy()) > 2,
        ["bid_depth_quote", "ask_depth_quote"],
    ] = np.nan

    # Execution quotes are never forward-filled: they are the first actual BBO
    # event after a known decision boundary.
    x = state.join(first_book.reindex(index))

    if trades.empty:
        x["buy_quote"] = 0.0
        x["sell_quote"] = 0.0
    else:
        quote = (
            pd.to_numeric(trades["price"], errors="coerce")
            * pd.to_numeric(trades["quantity"], errors="coerce")
        )
        buyer_maker = trades["is_buyer_maker"].astype(bool)
        buy = quote.where(~buyer_maker, 0.0).groupby(trades["sec"]).sum()
        sell = quote.where(buyer_maker, 0.0).groupby(trades["sec"]).sum()
        x["buy_quote"] = buy.reindex(index, fill_value=0.0)
        x["sell_quote"] = sell.reindex(index, fill_value=0.0)

    x["liq_buy_quote"] = 0.0
    x["liq_sell_quote"] = 0.0
    if not liquid.empty:
        liq_quote = (
            pd.to_numeric(liquid["price"], errors="coerce")
            * pd.to_numeric(liquid["quantity"], errors="coerce")
        )
        side = liquid["side"].astype(str).str.upper()
        x["liq_buy_quote"] = (
            liq_quote.where(side == "BUY", 0.0)
            .groupby(liquid["sec"])
            .sum()
            .reindex(index, fill_value=0.0)
        )
        x["liq_sell_quote"] = (
            liq_quote.where(side == "SELL", 0.0)
            .groupby(liquid["sec"])
            .sum()
            .reindex(index, fill_value=0.0)
        )

    if not mark.empty:
        keep = [c for c in ("mark_price", "index_price", "funding_rate") if c in mark]
        mark_state = (
            mark.sort_values("timestamp_ms", kind="mergesort")
            .groupby("sec", sort=True)
            .tail(1)
            .set_index("sec")[keep]
        )
        x = x.join(mark_state.reindex(index).ffill())

    x["mid"] = (x.bid_price + x.ask_price) / 2.0
    x["spread_bps"] = (x.ask_price / x.bid_price - 1.0) * 10000.0
    x["microprice"] = (
        x.ask_price * x.bid_qty + x.bid_price * x.ask_qty
    ) / (x.bid_qty + x.ask_qty).replace(0, np.nan)
    x["book_imbalance"] = (
        x.bid_depth_quote - x.ask_depth_quote
    ) / (x.bid_depth_quote + x.ask_depth_quote).replace(0, np.nan)
    x["trade_flow"] = (
        x.buy_quote - x.sell_quote
    ) / (x.buy_quote + x.sell_quote).replace(0, np.nan)
    x["trade_flow"] = x.trade_flow.fillna(0.0)
    x["signed_liq"] = x.liq_buy_quote - x.liq_sell_quote
    x["abs_liq"] = x.liq_buy_quote + x.liq_sell_quote
    x["ret_1s_bps"] = np.log(x.mid / x.mid.shift(1)) * 10000.0
    x["ret_5s_bps"] = np.log(x.mid / x.mid.shift(5)) * 10000.0
    buy5 = x.buy_quote.rolling(5).sum()
    sell5 = x.sell_quote.rolling(5).sum()
    x["flow_5s"] = (buy5 - sell5) / (buy5 + sell5).replace(0, np.nan)
    x["liq_5s"] = x.abs_liq.rolling(5).sum()
    x["liq_signed_5s"] = x.signed_liq.rolling(5).sum()
    x["liq_z"] = prior_z(np.log1p(x.liq_5s))
    x["flow_z"] = prior_z(x.flow_5s)
    x["spread_z"] = prior_z(x.spread_bps)
    x["depth_z"] = prior_z(np.log1p(x.bid_depth_quote + x.ask_depth_quote))
    return x


def rising_events(x: pd.DataFrame, threshold: float, cooldown: int = 60) -> np.ndarray:
    mask = (x.liq_z >= threshold) & (x.liq_5s > 0) & np.isfinite(x.mid)
    edge = mask & ~mask.shift(1, fill_value=False)
    raw = np.flatnonzero(edge.to_numpy())
    selected: list[int] = []
    next_allowed = -1
    for position in raw:
        if position >= next_allowed:
            selected.append(int(position))
            next_allowed = int(position) + cooldown
    return np.asarray(selected, dtype=np.int64)


def first_execution_position(x: pd.DataFrame, target_ms: int) -> int | None:
    # The first quote in ceil(target_ms / 1s) is guaranteed not to precede the
    # target. Search at most two seconds, matching the frozen latency bound.
    target_sec = (int(target_ms) + 999) // 1000
    start_pos = int(np.searchsorted(x.index.to_numpy(np.int64), target_sec, side="left"))
    for pos in range(start_pos, min(start_pos + 3, len(x))):
        observed = x.first_book_time_ms.iloc[pos]
        if not np.isfinite(observed):
            continue
        observed_ms = int(observed)
        if observed_ms >= target_ms and observed_ms - target_ms <= MAX_EXEC_DELAY_MS:
            return pos
    return None


def outcome(
    x: pd.DataFrame,
    event_i: int,
    policy: Policy,
    extra_cost_bps: float = BASE_EXTRA_COST_BPS,
) -> tuple[int, int, float] | None:
    decision_i = event_i + policy.confirm_s
    if decision_i + 1 >= len(x):
        return None
    liq_side = int(np.sign(x.liq_signed_5s.iloc[event_i]))
    if liq_side == 0:
        return None

    pre_i = max(event_i - 1, 0)
    pre_bid = float(x.bid_depth_quote.iloc[pre_i])
    pre_ask = float(x.ask_depth_quote.iloc[pre_i])
    if liq_side > 0:
        vulnerable_pre = pre_ask
        vulnerable_post = float(x.ask_depth_quote.iloc[decision_i])
    else:
        vulnerable_pre = pre_bid
        vulnerable_post = float(x.bid_depth_quote.iloc[decision_i])
    if (
        not np.isfinite(vulnerable_pre)
        or vulnerable_pre <= 0
        or not np.isfinite(vulnerable_post)
    ):
        return None

    depth_ratio = vulnerable_post / vulnerable_pre
    flow_align = liq_side * float(x.flow_z.iloc[decision_i])
    event_mid = float(x.mid.iloc[event_i])
    decision_mid = float(x.mid.iloc[decision_i])
    if min(event_mid, decision_mid) <= 0 or not np.isfinite(flow_align):
        return None
    event_to_decision = liq_side * math.log(decision_mid / event_mid) * 10000.0
    micro_align = (
        liq_side
        * (float(x.microprice.iloc[decision_i]) / decision_mid - 1.0)
        * 10000.0
    )

    if policy.family == "CONTINUATION":
        if (
            depth_ratio > policy.depth_ratio
            or flow_align < policy.flow_z
            or event_to_decision < policy.reclaim_bps
            or micro_align < 0
        ):
            return None
        trade_side = liq_side
    else:
        if (
            depth_ratio < policy.depth_ratio
            or flow_align > -policy.flow_z
            or event_to_decision > -policy.reclaim_bps
            or micro_align > 0
        ):
            return None
        trade_side = -liq_side

    decision_known_ms = (int(x.index[decision_i]) + 1) * 1000
    entry_i = first_execution_position(x, decision_known_ms)
    if entry_i is None:
        return None
    entry_time_ms = int(x.first_book_time_ms.iloc[entry_i])
    exit_i = first_execution_position(x, entry_time_ms + policy.horizon_s * 1000)
    if exit_i is None:
        return None

    entry = float(
        x.first_ask_price.iloc[entry_i]
        if trade_side > 0
        else x.first_bid_price.iloc[entry_i]
    )
    exit_price = float(
        x.first_bid_price.iloc[exit_i]
        if trade_side > 0
        else x.first_ask_price.iloc[exit_i]
    )
    if min(entry, exit_price) <= 0 or not np.isfinite(entry + exit_price):
        return None
    net_bps = trade_side * math.log(exit_price / entry) * 10000.0 - extra_cost_bps
    return int(x.index[event_i]), int(x.index[exit_i]), float(net_bps)


def evaluate_interval(
    panels: list[pd.DataFrame],
    policy: Policy,
    start_sec: int,
    end_sec: int,
) -> list[tuple[int, int, float]]:
    # One global BTC exposure slot. The same admitted event set is later replayed
    # under every cost profile by subtracting additional costs only.
    results: list[tuple[int, int, float]] = []
    free_at = -10**30
    for x in panels:
        for event_i in rising_events(x, policy.liq_z):
            event_sec = int(x.index[int(event_i)])
            if event_sec < start_sec or event_sec >= end_sec or event_sec < free_at:
                continue
            result = outcome(x, int(event_i), policy, BASE_EXTRA_COST_BPS)
            if result is None:
                continue
            _, exit_sec, _ = result
            if exit_sec >= end_sec:
                continue
            results.append(result)
            free_at = exit_sec
    return results


def summarize(events: list[tuple[int, int, float]], added_cost_bps: float = 0.0) -> dict:
    if not events:
        return {
            "n": 0,
            "mean_bps": math.nan,
            "top5_removed_bps": math.nan,
            "win_rate": math.nan,
        }
    values = np.asarray([row[2] - added_cost_bps for row in events], dtype=float)
    return {
        "n": int(len(values)),
        "mean_bps": float(values.mean()),
        "top5_removed_bps": trimmed_mean(values, 5),
        "win_rate": float((values > 0).mean()),
    }


def build_policies() -> list[Policy]:
    policies: list[Policy] = []
    for family in ("CONTINUATION", "REVERSAL"):
        depth_values = (0.8, 1.0) if family == "CONTINUATION" else (1.0, 1.3)
        for liq_z in (2.5, 3.5):
            for confirm_s in (5, 15):
                for depth_ratio in depth_values:
                    for flow_z in (0.0, 0.75):
                        for reclaim_bps in (0.0, 2.0):
                            for horizon_s in (30, 120, 300):
                                policies.append(
                                    Policy(
                                        family,
                                        liq_z,
                                        confirm_s,
                                        depth_ratio,
                                        flow_z,
                                        reclaim_bps,
                                        horizon_s,
                                    )
                                )
    return policies


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    local = Path(
        snapshot_download(
            REPO_ID,
            repo_type="dataset",
            revision=DATASET_REVISION,
            allow_patterns=[f"{stream}/**/*.parquet" for stream in STREAMS],
        )
    )
    common: set[str] | None = None
    for stream in STREAMS:
        dates = dates_under(local, stream)
        common = dates if common is None else common & dates
    days = sorted(common or set())
    if not days:
        raise RuntimeError(f"no common dates under {local}")

    panels: list[pd.DataFrame] = []
    audit: list[dict] = []
    for day in days:
        panel = aggregate_day(local, day)
        if panel.empty:
            continue
        panels.append(panel)
        audit.append(
            {
                "day": day,
                "rows": int(len(panel)),
                "liq_event_seconds": int((panel.abs_liq > 0).sum()),
                "start_sec": int(panel.index.min()),
                "end_sec": int(panel.index.max()),
            }
        )
        print("loaded", day, len(panel), audit[-1]["liq_event_seconds"], flush=True)
    if not panels:
        raise RuntimeError("no usable panels")

    all_seconds = np.concatenate([panel.index.to_numpy(np.int64) for panel in panels])
    dev_end = int(np.quantile(all_seconds, 0.40))
    val_end = int(np.quantile(all_seconds, 0.70))
    policies = build_policies()
    rows: list[dict] = []

    # Confirmation labels are not accessed anywhere in this loop.
    for policy in policies:
        dev_events = evaluate_interval(panels, policy, -10**30, dev_end)
        val_events = evaluate_interval(panels, policy, dev_end, val_end)
        record = {**asdict(policy), "policy_id": policy.policy_id}
        for tag, events in (("dev", dev_events), ("val", val_events)):
            for cost, cost_tag in (
                (10.0, "10bp"),
                (15.0, "15bp"),
                (20.0, "20bp"),
            ):
                stats = summarize(events, cost - BASE_EXTRA_COST_BPS)
                for key, value in stats.items():
                    record[f"{tag}_{key}_{cost_tag}"] = value
        score_terms = [
            record["dev_mean_bps_15bp"],
            record["val_mean_bps_15bp"],
            record["dev_top5_removed_bps_15bp"],
            record["val_top5_removed_bps_15bp"],
            record["dev_mean_bps_20bp"],
            record["val_mean_bps_20bp"],
        ]
        record["selection_score"] = (
            float(min(score_terms)) if all(np.isfinite(score_terms)) else -1e9
        )
        record["eligible"] = bool(
            record["dev_n_10bp"] >= 20
            and record["val_n_10bp"] >= 15
            and record["selection_score"] > 0
        )
        rows.append(record)

    grid = pd.DataFrame(rows).sort_values(
        ["eligible", "selection_score", "policy_id"],
        ascending=[False, False, True],
        kind="mergesort",
    )
    grid.to_csv(args.out / "DEV_VALIDATION_GRID.csv", index=False)
    eligible = grid.loc[grid.eligible].copy()
    eligible.to_csv(args.out / "ELIGIBLE_DEV_VALIDATION.csv", index=False)

    selected = eligible.iloc[0].to_dict() if len(eligible) else None
    confirmation_opened = bool(selected is not None)
    confirmation: dict | None = None
    confirmation_passed = False
    if selected is not None:
        policy = Policy(
            family=str(selected["family"]),
            liq_z=float(selected["liq_z"]),
            confirm_s=int(selected["confirm_s"]),
            depth_ratio=float(selected["depth_ratio"]),
            flow_z=float(selected["flow_z"]),
            reclaim_bps=float(selected["reclaim_bps"]),
            horizon_s=int(selected["horizon_s"]),
        )
        confirm_events = evaluate_interval(panels, policy, val_end, 10**30)
        confirmation = {
            "policy": asdict(policy),
            "policy_id": policy.policy_id,
            "10bp": summarize(confirm_events, 0.0),
            "15bp": summarize(confirm_events, 5.0),
            "20bp": summarize(confirm_events, 10.0),
        }
        confirmation_passed = bool(
            confirmation["15bp"]["n"] >= 15
            and confirmation["15bp"]["mean_bps"] > 0
            and confirmation["15bp"]["top5_removed_bps"] > 0
            and confirmation["20bp"]["mean_bps"] > 0
        )
        pd.DataFrame(
            confirm_events,
            columns=["event_sec", "exit_sec", "net_bps_10bp"],
        ).to_csv(args.out / "CONFIRMATION_LEDGER.csv", index=False)

    result = {
        "status": "COMPLETE",
        "dataset": REPO_ID,
        "dataset_revision": DATASET_REVISION,
        "days": days,
        "audit": audit,
        "split": {"dev_end_sec": dev_end, "validation_end_sec": val_end},
        "policy_count": len(policies),
        "eligible_dev_validation_count": int(len(eligible)),
        "selected_without_confirmation": selected,
        "confirmation_opened": confirmation_opened,
        "confirmation": confirmation,
        "confirmation_passed": confirmation_passed,
        "execution_contract": {
            "features": "last actual quote/depth in completed second; <=2s state age",
            "decision": "after completed confirmation second",
            "entry": "first actual BBO event at/after decision boundary; <=2s",
            "exit": "first actual BBO event at/after entry timestamp plus horizon; <=2s",
            "spread": "ask-to-bid for long and bid-to-ask for short",
            "extra_roundtrip_cost_bps": [10.0, 15.0, 20.0],
            "global_positions": 1,
            "same_signal_set_across_costs": True,
        },
        "orders_submitted": False,
        "paper_or_live_started": False,
        "promotion_allowed": False,
    }
    (args.out / "SUMMARY.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "days": days,
                "policy_count": len(policies),
                "eligible_dev_validation_count": int(len(eligible)),
                "confirmation_opened": confirmation_opened,
                "confirmation_passed": confirmation_passed,
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

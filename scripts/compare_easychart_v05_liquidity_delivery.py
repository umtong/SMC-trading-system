from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from ictbt.easychart_v0.domain import (
    B1Subtype,
    ConfluenceAuthority,
    LiquidityDeliveryAuthority,
)
from ictbt.easychart_v0.execution import RiskConfig
from ictbt.easychart_v0.pipeline import build_feature_book
from ictbt.easychart_v0.v04 import (
    build_baseline_event_authorities,
    build_preexisting_structure_authorities,
    run_v04_historical_replay,
)
from ictbt.easychart_v0.v05 import build_m15_m5_liquidity_delivery_result

from analyze_easychart_v0_2_winrate import COSTS, WINDOWS


ARMS = (
    "A_V03_FIRST_REVISIT_LOCKED",
    "B_V04_PREEXISTING_VALID_EPISODE_LOCKED",
    "C_V05_M15_SWEEP_DELIVERY_FIRST_REVISIT",
    "D_V03_PLUS_V05_SINGLE_SLOT",
    "E_V03_BREAK_RETEST_PLUS_V05_SINGLE_SLOT",
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/s4_continuous_2021_2026"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results/easychart_v05_m15_m5_liquidity_delivery"),
    )
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.03)
    parser.add_argument("--window-index", type=int)
    return parser.parse_args()


def _load_source(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    timestamp_column = next(
        name
        for name in ("open_time", "timestamp", "time", "datetime")
        if name in frame.columns
    )
    frame.index = pd.DatetimeIndex(
        pd.to_datetime(frame.pop(timestamp_column), utc=True),
        name="open_time",
    )
    return frame


def _window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    begin = pd.Timestamp(start, tz="UTC")
    finish = pd.Timestamp(end, tz="UTC")
    return frame.loc[(frame.index >= begin) & (frame.index < finish)].copy()


def _mfe_r(candles: pd.DataFrame, trade) -> float:
    risk_distance = abs(trade.entry_price - trade.initial_stop)
    if risk_distance <= 0:
        return 0.0
    bars = candles.loc[
        (candles.index >= trade.entry_time)
        & (candles.index <= trade.closed_at)
    ]
    if bars.empty:
        return 0.0
    favorable = (
        float(bars["high"].max()) - trade.entry_price
        if trade.side.value == "long"
        else trade.entry_price - float(bars["low"].min())
    )
    return max(0.0, favorable / risk_distance)


def _trade_row(
    *,
    arm: str,
    window_index: int,
    symbol: str,
    environment: str,
    candles: pd.DataFrame,
    attempt,
    authority,
) -> dict[str, object]:
    trade = attempt.result.trade
    assert trade is not None
    subtype = None
    delivery_kind = None
    stop_owner = None
    entry_zone_source = None
    sweep_to_delivery_minutes = None
    delivery_to_entry_minutes = None
    location_id = getattr(authority, "location_id", None)
    if isinstance(authority, ConfluenceAuthority):
        subtype = authority.confirmation.subtype.value
    elif isinstance(authority, LiquidityDeliveryAuthority):
        subtype = authority.delivery_kind
        delivery_kind = authority.delivery_kind
        stop_owner = authority.stop_owner
        entry_zone_source = authority.entry_zone_source
        sweep_to_delivery_minutes = (
            authority.known_at - authority.liquidity_event.known_at
        ).total_seconds() / 60
        delivery_to_entry_minutes = (
            trade.entry_time - authority.known_at
        ).total_seconds() / 60
    return {
        "arm": arm,
        "window_index": window_index,
        "symbol": symbol,
        "environment": environment,
        "scene_family": trade.scene_family.value,
        "subtype": subtype,
        "delivery_kind": delivery_kind,
        "stop_owner": stop_owner,
        "entry_zone_source": entry_zone_source,
        "authority_id": attempt.authority_id,
        "location_id": location_id,
        "side": trade.side.value,
        "created_at": attempt.intent.created_at.isoformat(),
        "entry_time": trade.entry_time.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "entry_price": trade.entry_price,
        "initial_stop": trade.initial_stop,
        "initial_target": trade.initial_target,
        "stop_distance": abs(trade.entry_price - trade.initial_stop),
        "target_distance": abs(trade.initial_target - trade.entry_price),
        "target_r": trade.target_r,
        "mfe_r": _mfe_r(candles, trade),
        "partial_1r": any(
            leg.reason == "partial_1r" for leg in trade.exit_legs
        ),
        "final_reason": trade.final_reason,
        "risk_budget": attempt.intent.risk_budget,
        "net_r": trade.net_pnl / attempt.intent.risk_budget,
        "net_pnl": trade.net_pnl,
        "equity_before": attempt.equity_before,
        "equity_after": attempt.equity_after,
        "sweep_to_delivery_minutes": sweep_to_delivery_minutes,
        "delivery_to_entry_minutes": delivery_to_entry_minutes,
    }


def _max_drawdown_fraction(
    rows: list[dict[str, object]],
    initial_equity: float,
) -> float:
    worst = 0.0
    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["window_index"])].append(row)
    for trades in grouped.values():
        peak = initial_equity
        for row in sorted(trades, key=lambda item: str(item["closed_at"])):
            equity = float(row["equity_after"])
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
    return worst


def _metrics(
    rows: list[dict[str, object]],
    *,
    initial_equity: float,
    window_count: int,
) -> dict[str, object]:
    pnls = [float(row["net_pnl"]) for row in rows]
    net_rs = [float(row["net_r"]) for row in rows]
    target_rs = [float(row["target_r"]) for row in rows]
    wins_r = [value for value in net_rs if value > 0]
    losses_r = [value for value in net_rs if value < 0]
    gross_win = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    average_win = None if not wins_r else statistics.fmean(wins_r)
    average_loss = None if not losses_r else statistics.fmean(losses_r)
    break_even = (
        None
        if average_win is None or average_loss is None
        else abs(average_loss) / (average_win + abs(average_loss))
    )
    return {
        "trades": len(rows),
        "trades_per_instrument_day": len(rows) / (14 * window_count),
        "wins": len(wins_r),
        "losses": len(losses_r),
        "win_rate": None if not rows else len(wins_r) / len(rows),
        "net_pnl": sum(pnls),
        "return_on_panel_capital": (
            sum(pnls) / (initial_equity * window_count)
        ),
        "net_r": sum(net_rs),
        "average_net_r": None if not rows else statistics.fmean(net_rs),
        "profit_factor": (
            None
            if gross_loss == 0
            else gross_win / gross_loss
        ),
        "average_win_r": average_win,
        "average_loss_r": average_loss,
        "break_even_win_rate": break_even,
        "average_target_r": (
            None if not target_rs else statistics.fmean(target_rs)
        ),
        "median_target_r": (
            None if not target_rs else statistics.median(target_rs)
        ),
        "median_stop_distance": (
            None
            if not rows
            else statistics.median(float(row["stop_distance"]) for row in rows)
        ),
        "mfe_at_least_1r": sum(float(row["mfe_r"]) >= 1 for row in rows),
        "partial_1r_trades": sum(bool(row["partial_1r"]) for row in rows),
        "partial_then_breakeven": sum(
            bool(row["partial_1r"]) and row["final_reason"] == "initial_stop"
            for row in rows
        ),
        "volume_exits": sum(
            row["final_reason"] == "volume_spike" for row in rows
        ),
        "max_drawdown_fraction": _max_drawdown_fraction(rows, initial_equity),
    }


def main() -> int:
    args = _args()
    indices = (
        (args.window_index,)
        if args.window_index is not None
        else tuple(range(len(WINDOWS)))
    )
    if any(index < 0 or index >= len(WINDOWS) for index in indices):
        raise SystemExit("window-index out of range")

    risk = RiskConfig(
        risk_fraction=args.risk_fraction,
        quantity_step=0.001,
        daily_loss_limit_enabled=False,
    )
    frames = {
        symbol: _load_source(args.data_dir / f"{symbol}_5m.csv")
        for symbol in {WINDOWS[index][0] for index in indices}
    }
    ledger: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []

    for ordinal, index in enumerate(indices, start=1):
        symbol, environment, start, end, tick_size = WINDOWS[index]
        candles = _window(frames[symbol], start, end)
        print(
            f"[{ordinal}/{len(indices)}] {symbol} {environment}: "
            f"{len(candles)} bars",
            flush=True,
        )
        book = build_feature_book(candles, symbol=symbol, tick_size=tick_size)
        baseline_authorities = build_baseline_event_authorities(book)
        baseline_break_retest = tuple(
            authority
            for authority in baseline_authorities
            if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
        )
        preexisting_authorities = build_preexisting_structure_authorities(book)
        v05_result = build_m15_m5_liquidity_delivery_result(book)
        arm_inputs = (
            (ARMS[0], baseline_authorities, True),
            (ARMS[1], preexisting_authorities, False),
            (ARMS[2], v05_result.authorities, False),
            (
                ARMS[3],
                tuple(
                    sorted(
                        (
                            *baseline_authorities,
                            *v05_result.authorities,
                        ),
                        key=lambda item: (item.known_at, item.authority_id),
                    )
                ),
                None,
            ),
            (
                ARMS[4],
                tuple(
                    sorted(
                        (*baseline_break_retest, *v05_result.authorities),
                        key=lambda item: (item.known_at, item.authority_id),
                    )
                ),
                None,
            ),
        )
        for arm, authorities, use_v03_targets in arm_inputs:
            run = run_v04_historical_replay(
                candles,
                symbol=symbol,
                tick_size=tick_size,
                equity=args.initial_equity,
                costs=COSTS,
                risk=risk,
                book=book,
                authorities=authorities,
                use_v03_targets=use_v03_targets,
            )
            authority_map = {
                authority.authority_id: authority for authority in run.authorities
            }
            for attempt in run.attempts:
                if attempt.result.trade is None:
                    continue
                ledger.append(
                    _trade_row(
                        arm=arm,
                        window_index=index,
                        symbol=symbol,
                        environment=environment,
                        candles=candles,
                        attempt=attempt,
                        authority=authority_map[attempt.authority_id],
                    )
                )
            cancellation_reasons = Counter(
                item.reason for item in run.pending_cancellations
            )
            row: dict[str, object] = {
                "arm": arm,
                "window_index": index,
                "symbol": symbol,
                "environment": environment,
                "authorities": len(run.authorities),
                "attempts": len(run.attempts),
                "closed_trades": len(run.closed_trades),
                "opportunity_rejections": len(run.opportunity_rejections),
                "sizing_rejections": len(run.sizing_rejections),
                "pending_cancellations": len(run.pending_cancellations),
                "slot_suppressed_authorities": run.slot_suppressed_authorities,
                "open_at_data_end": sum(
                    attempt.result.open_position is not None
                    for attempt in run.attempts
                ),
                "cancellation_reasons": json.dumps(
                    cancellation_reasons,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "final_equity": run.final_equity,
            }
            if arm == ARMS[2]:
                row.update(
                    {
                        f"funnel_{key}": value
                        for key, value in asdict(v05_result.diagnostics).items()
                    }
                )
            diagnostics.append(row)
            print(
                f"  {arm}: authorities={len(run.authorities)}, "
                f"closed={len(run.closed_trades)}, "
                f"equity={run.final_equity:.2f}",
                flush=True,
            )

    summary = {
        "contract": {
            "windows": len(indices),
            "instrument_days": 14 * len(indices),
            "initial_equity_per_window": args.initial_equity,
            "risk_fraction": args.risk_fraction,
            "daily_loss_limit_enabled": False,
            "one_total_slot": True,
            "entry_mode": "limit_first_revisit",
            "costs": {
                "entry_fee_rate": COSTS.entry_fee_rate,
                "stop_fee_rate": COSTS.stop_fee_rate,
                "target_fee_rate": COSTS.target_fee_rate,
                "volume_exit_fee_rate": COSTS.volume_exit_fee_rate,
                "stop_slippage_bps": COSTS.stop_slippage_bps,
                "volume_exit_slippage_bps": COSTS.volume_exit_slippage_bps,
            },
        },
        "arms": {
            arm: _metrics(
                [row for row in ledger if row["arm"] == arm],
                initial_equity=args.initial_equity,
                window_count=len(indices),
            )
            for arm in ARMS
        },
        "by_environment": {
            arm: {
                environment: _metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm
                        and row["environment"] == environment
                    ],
                    initial_equity=args.initial_equity,
                    window_count=1,
                )
                for environment in sorted(
                    {WINDOWS[index][1] for index in indices}
                )
            }
            for arm in ARMS
        },
        "by_scene_or_subtype": {
            arm: {
                key: _metrics(
                    [
                        row
                        for row in ledger
                        if row["arm"] == arm
                        and str(row["subtype"] or row["scene_family"]) == key
                    ],
                    initial_equity=args.initial_equity,
                    window_count=len(indices),
                )
                for key in sorted(
                    {
                        str(row["subtype"] or row["scene_family"])
                        for row in ledger
                        if row["arm"] == arm
                    }
                )
            }
            for arm in ARMS
        },
        "v05_by_delivery_kind": {
            kind: _metrics(
                [
                    row
                    for row in ledger
                    if row["arm"] == ARMS[2]
                    and row["delivery_kind"] == kind
                ],
                initial_equity=args.initial_equity,
                window_count=len(indices),
            )
            for kind in ("ob", "ob_fvg", "fvg")
        },
        "v05_by_stop_owner": {
            owner: _metrics(
                [
                    row
                    for row in ledger
                    if row["arm"] == ARMS[2]
                    and row["stop_owner"] == owner
                ],
                initial_equity=args.initial_equity,
                window_count=len(indices),
            )
            for owner in (
                "m5_ob_formation",
                "m5_fvg_formation",
                "m15_event",
            )
        },
        "diagnostics": diagnostics,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    pd.DataFrame.from_records(ledger).to_csv(
        args.output_dir / "trade_ledger.csv",
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame.from_records(diagnostics).to_csv(
        args.output_dir / "diagnostics.csv",
        index=False,
        encoding="utf-8-sig",
    )
    print(json.dumps(summary["arms"], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from ictbt.easychart_v0.application import run_historical_replay
from ictbt.easychart_v0.domain import ConfluenceAuthority, SceneFamily
from ictbt.easychart_v0.execution import RiskConfig
from ictbt.easychart_v0.pipeline import build_feature_book
from ictbt.easychart_v0.v04 import (
    V04Policy,
    build_baseline_event_authorities,
    build_corrected_event_authorities,
    build_preexisting_structure_authorities,
    run_v04_historical_replay,
)

from analyze_easychart_v0_2_winrate import COSTS, WINDOWS


ARMS = (
    "v03_first_revisit_baseline",
    "v04_corrected_event",
    "v04_preexisting_structure",
    "v04_combined",
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
        default=Path("results/easychart_v04_scene_family_comparison"),
    )
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--risk-fraction", type=float, default=0.03)
    parser.add_argument("--window-index", type=int)
    return parser.parse_args()


def _load_source(path: Path) -> pd.DataFrame:
    """Load once without validating millions of rows outside the fixed windows."""

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


def _metrics(rows: list[dict[str, object]], initial_equity_total: float) -> dict[str, object]:
    pnls = [float(row["net_pnl"]) for row in rows]
    net_rs = [float(row["net_r"]) for row in rows]
    wins = sum(value > 0 for value in pnls)
    losses = sum(value < 0 for value in pnls)
    gross_win = sum(value for value in pnls if value > 0)
    gross_loss = -sum(value for value in pnls if value < 0)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": None if not rows else wins / len(rows),
        "net_pnl": sum(pnls),
        "return_on_panel_capital": sum(pnls) / initial_equity_total,
        "net_r": sum(net_rs),
        "average_net_r": None if not rows else sum(net_rs) / len(rows),
        "profit_factor": (
            None
            if gross_loss == 0 and gross_win == 0
            else None
            if gross_loss == 0
            else gross_win / gross_loss
        ),
        "average_target_r": (
            None
            if not rows
            else sum(float(row["target_r"]) for row in rows) / len(rows)
        ),
        "partial_1r_trades": sum(bool(row["partial_1r"]) for row in rows),
    }


def _trade_row(
    *,
    arm: str,
    window_index: int,
    symbol: str,
    environment: str,
    attempt,
    authority=None,
) -> dict[str, object]:
    trade = attempt.result.trade
    assert trade is not None
    subtype = None
    if isinstance(authority, ConfluenceAuthority):
        subtype = authority.confirmation.subtype.value
    return {
        "arm": arm,
        "window_index": window_index,
        "symbol": symbol,
        "environment": environment,
        "scene_family": trade.scene_family.value,
        "subtype": subtype,
        "authority_id": attempt.authority_id,
        "side": trade.side.value,
        "created_at": attempt.intent.created_at.isoformat(),
        "entry_time": trade.entry_time.isoformat(),
        "closed_at": trade.closed_at.isoformat(),
        "entry_price": trade.entry_price,
        "initial_stop": trade.initial_stop,
        "initial_target": trade.initial_target,
        "target_r": trade.target_r,
        "partial_1r": any(leg.reason == "partial_1r" for leg in trade.exit_legs),
        "final_reason": trade.final_reason,
        "risk_budget": attempt.intent.risk_budget,
        "net_r": trade.net_pnl / attempt.intent.risk_budget,
        "net_pnl": trade.net_pnl,
        "equity_before": attempt.equity_before,
        "equity_after": attempt.equity_after,
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

    for index in indices:
        symbol, environment, start, end, tick_size = WINDOWS[index]
        candles = _window(frames[symbol], start, end)
        print(
            f"[{index + 1}/{len(indices)}] {symbol} {environment}: "
            f"{len(candles)} bars",
            flush=True,
        )

        book = build_feature_book(candles, symbol=symbol, tick_size=tick_size)
        baseline_authorities = build_baseline_event_authorities(book)
        corrected_authorities = build_corrected_event_authorities(book)
        structure_authorities = build_preexisting_structure_authorities(book)
        arm_inputs = (
            (ARMS[0], baseline_authorities, True),
            (ARMS[1], corrected_authorities, False),
            (ARMS[2], structure_authorities, False),
            (
                ARMS[3],
                tuple(
                    sorted(
                        (*corrected_authorities, *structure_authorities),
                        key=lambda item: (
                            item.known_at,
                            0
                            if item.scene_family
                            is SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST
                            else 1,
                            item.zone.width,
                            item.authority_id,
                        ),
                    )
                ),
                False,
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
                if attempt.result.trade is not None:
                    ledger.append(
                        _trade_row(
                            arm=arm,
                            window_index=index,
                            symbol=symbol,
                            environment=environment,
                            attempt=attempt,
                            authority=authority_map.get(attempt.authority_id),
                        )
                    )
            diagnostics.append(
                {
                    "arm": arm,
                    "window_index": index,
                    "authorities": len(run.authorities),
                    "attempts": len(run.attempts),
                    "opportunity_rejections": len(run.opportunity_rejections),
                    "sizing_rejections": len(run.sizing_rejections),
                    "pending_cancellations": len(run.pending_cancellations),
                    "final_equity": run.final_equity,
                }
            )
            print(
                f"  {arm}: authorities={len(run.authorities)}, "
                f"closed={len(run.closed_trades)}, equity={run.final_equity:.2f}",
                flush=True,
            )

    panel_capital = args.initial_equity * len(indices)
    summary = {
        "contract": {
            "windows": len(indices),
            "instrument_days": 14 * len(indices),
            "initial_equity_per_window": args.initial_equity,
            "risk_fraction": args.risk_fraction,
            "daily_loss_limit_enabled": False,
            "one_slot": True,
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
                panel_capital,
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
                    args.initial_equity,
                )
                for environment in sorted(
                    {WINDOWS[index][1] for index in indices}
                )
            }
            for arm in ARMS
        },
        "by_scene_or_subtype": {},
        "diagnostics": diagnostics,
    }

    breakdown: dict[str, dict[str, list[dict[str, object]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in ledger:
        key = str(row["subtype"] or row["scene_family"])
        breakdown[str(row["arm"])][key].append(row)
    summary["by_scene_or_subtype"] = {
        arm: {
            key: _metrics(rows, panel_capital)
            for key, rows in groups.items()
        }
        for arm, groups in breakdown.items()
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





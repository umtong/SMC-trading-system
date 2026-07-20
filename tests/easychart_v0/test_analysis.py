from __future__ import annotations

import math

import pandas as pd

from ictbt.easychart_v0.analysis import analyze_historical_replay
from ictbt.easychart_v0.application import HistoricalReplayRun, ReplayAttempt
from ictbt.easychart_v0.domain import (
    EntryMode,
    FormationBar,
    ObKind,
    OrderBlock,
    PriceZone,
    SceneFamily,
    Side,
    Timeframe,
)
from ictbt.easychart_v0.execution import OrderIntent, TradeRecord
from ictbt.easychart_v0.pipeline import FeatureBook
from ictbt.easychart_v0.replay import ReplayResult


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def _block(ob_id: str, timeframe: Timeframe, known_at: str) -> OrderBlock:
    delta = {
        Timeframe.M5: pd.Timedelta(minutes=5),
        Timeframe.M15: pd.Timedelta(minutes=15),
        Timeframe.H1: pd.Timedelta(hours=1),
        Timeframe.H4: pd.Timedelta(hours=4),
    }[timeframe]
    end = pd.Timestamp(known_at)
    first = end - 2 * delta
    return OrderBlock(
        ob_id=ob_id,
        symbol="BTCUSDT",
        timeframe=timeframe,
        kind=ObKind.SIMPLE_2C,
        side=Side.LONG,
        formation_bars=(
            FormationBar(first, first + delta, 101, 102, 98, 99, 10),
            FormationBar(first + delta, end, 99, 104, 97, 103, 20),
        ),
        zone=PriceZone(99, 102),
        known_at=end,
        stop_extreme=97,
        initial_stop=96,
        impulse_extreme=104,
    )


def _book() -> FeatureBook:
    h1 = _block("h1-location", Timeframe.H1, "2026-01-01T01:00:00Z")
    m15_confirmation = _block(
        "m15-confirmation", Timeframe.M15, "2026-01-01T01:15:00Z"
    )
    m15_location = _block(
        "m15-location", Timeframe.M15, "2026-01-01T02:00:00Z"
    )
    m5_confirmation = _block(
        "m5-confirmation", Timeframe.M5, "2026-01-01T02:05:00Z"
    )
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.5,
        frames={timeframe: _empty_frame() for timeframe in Timeframe},
        order_blocks={
            Timeframe.M5: (m5_confirmation,),
            Timeframe.M15: (m15_confirmation, m15_location),
            Timeframe.H1: (h1,),
            Timeframe.H4: (),
        },
        pivots={timeframe: () for timeframe in Timeframe},
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def _attempt(
    number: int,
    *,
    authority_id: str,
    side: Side,
    pnl: float,
    final_reason: str,
) -> ReplayAttempt:
    timestamp = pd.Timestamp("2026-01-01T03:00:00Z") + pd.Timedelta(
        minutes=number
    )
    stop, target = ((90.0, 110.0) if side is Side.LONG else (110.0, 90.0))
    intent = OrderIntent(
        order_id=f"order-{number}",
        source_id=authority_id,
        symbol="BTCUSDT",
        scene_family=SceneFamily.A1_B1_CONFLUENCE,
        side=side,
        entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
        created_at=timestamp - pd.Timedelta(minutes=1),
        entry_reference=100,
        initial_stop=stop,
        initial_target=target,
        risk_budget=100,
        unit_stop_risk=10,
        quantity=10,
    )
    trade = TradeRecord(
        order_id=intent.order_id,
        symbol="BTCUSDT",
        side=side,
        scene_family=SceneFamily.A1_B1_CONFLUENCE,
        entry_time=timestamp - pd.Timedelta(seconds=30),
        entry_price=100,
        initial_stop=stop,
        initial_target=target,
        original_quantity=10,
        target_r=1,
        exit_legs=(),
        entry_fee_paid=0,
        gross_pnl=pnl,
        fees_paid=0,
        net_pnl=pnl,
        closed_at=timestamp,
        final_reason=final_reason,
    )
    result = ReplayResult(
        status="CLOSED", intent=intent, events=(), trade=trade
    )
    return ReplayAttempt(
        opportunity_id=f"opportunity-{number}",
        authority_id=authority_id,
        intent=intent,
        result=result,
        equity_before=0,
        equity_after=0,
    )


def _run(attempts: tuple[ReplayAttempt, ...]) -> HistoricalReplayRun:
    return HistoricalReplayRun(
        book=_book(),
        decision_times=(),
        attempts=attempts,
        opportunity_rejections=(),
        sizing_rejections=(),
        initial_equity=1_000,
        final_equity=1_000 + sum(
            attempt.result.trade.net_pnl
            for attempt in attempts
            if attempt.result.trade is not None
        ),
    )


def test_analysis_aggregates_strategy_level_metrics() -> None:
    h1_m15 = "confluence:h1-location|b1-confirmation:m15-confirmation"
    m15_m5 = (
        "confluence:m15-location|m15-m5-delivery:event-1|mss-1|m5-confirmation"
    )
    run = _run(
        (
            _attempt(
                1,
                authority_id=h1_m15,
                side=Side.LONG,
                pnl=100,
                final_reason="initial_target",
            ),
            _attempt(
                2,
                authority_id=h1_m15,
                side=Side.SHORT,
                pnl=-40,
                final_reason="initial_stop",
            ),
            _attempt(
                3,
                authority_id=m15_m5,
                side=Side.LONG,
                pnl=-10,
                final_reason="volume_spike",
            ),
            _attempt(
                4,
                authority_id=m15_m5,
                side=Side.SHORT,
                pnl=20,
                final_reason="initial_target",
            ),
        )
    )

    report = analyze_historical_replay(run)

    assert report.total_trades == 4
    assert report.win_rate == 0.5
    assert report.net_pnl_total == 70
    assert report.net_pnl_mean == 17.5
    assert report.profit_factor == 2.4
    assert report.max_drawdown == 50
    assert report.final_reason_counts == {
        "initial_target": 2,
        "initial_stop": 1,
        "volume_spike": 1,
    }
    assert report.side_net_pnl == {"long": 90.0, "short": -20.0}
    assert report.confluence_pair_net_pnl == {
        "1h+15m": 60.0,
        "1h+5m": 0.0,
        "15m+5m": 10.0,
        "1h-pivot+15m": 0.0,
        "1h-pivot+5m": 0.0,
    }


def test_analysis_empty_and_no_loss_profit_factor_are_explicit() -> None:
    empty = analyze_historical_replay(_run(()))
    winner = analyze_historical_replay(
        _run(
            (
                _attempt(
                    1,
                    authority_id="confluence:h1-location|b1-confirmation:m15-confirmation",
                    side=Side.LONG,
                    pnl=10,
                    final_reason="initial_target",
                ),
            )
        )
    )

    assert empty.total_trades == 0
    assert empty.profit_factor is None
    assert empty.max_drawdown == 0
    assert math.isinf(winner.profit_factor)



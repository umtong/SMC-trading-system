from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from typing import Mapping

from .application import HistoricalReplayRun
from .domain import Timeframe


@dataclass(frozen=True, slots=True)
class ReplayAnalysis:
    total_trades: int
    win_rate: float
    net_pnl_total: float
    net_pnl_mean: float
    profit_factor: float | None
    max_drawdown: float
    final_reason_counts: Mapping[str, int]
    side_net_pnl: Mapping[str, float]
    confluence_pair_net_pnl: Mapping[str, float]


def _pair_by_authority(run: HistoricalReplayRun) -> dict[str, str]:
    blocks = {
        block.ob_id: timeframe
        for timeframe, items in run.book.order_blocks.items()
        for block in items
    }
    pivots = {
        pivot.pivot_id: pivot.timeframe
        for timeframe, items in run.book.pivots.items()
        for pivot in items
    }
    output: dict[str, str] = {}
    legacy_marker = "|b1-confirmation:"
    mtf_marker = "|m15-m5-delivery:"
    for attempt in run.attempts:
        if not attempt.authority_id.startswith("confluence:"):
            continue
        payload = attempt.authority_id.removeprefix("confluence:")
        location_id, separator, confirmation_id = payload.partition(legacy_marker)
        if not separator:
            location_id, separator, delivery_lineage = payload.partition(mtf_marker)
            if not separator:
                continue
            confirmation_id = delivery_lineage.rpartition("|")[2]
        location_timeframe = blocks.get(location_id) or pivots.get(location_id)
        confirmation_timeframe = blocks.get(confirmation_id)
        if location_timeframe is None or confirmation_timeframe is None:
            continue
        prefix = (
            f"{location_timeframe.value}-pivot"
            if location_id in pivots
            else location_timeframe.value
        )
        output[attempt.authority_id] = f"{prefix}+{confirmation_timeframe.value}"
    return output


def analyze_historical_replay(run: HistoricalReplayRun) -> ReplayAnalysis:
    """Aggregate completed trades without changing the replay or its account state."""

    completed = [
        (attempt, attempt.result.trade)
        for attempt in run.attempts
        if attempt.result.trade is not None
    ]
    trades = [trade for _, trade in completed]
    total = len(trades)
    net_values = [trade.net_pnl for trade in trades]
    gross_profit = sum(value for value in net_values if value > 0)
    gross_loss = -sum(value for value in net_values if value < 0)
    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = math.inf
    else:
        profit_factor = None

    equity = float(run.initial_equity)
    peak = equity
    max_drawdown = 0.0
    for trade in sorted(trades, key=lambda item: item.closed_at):
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    side_net_pnl = {"long": 0.0, "short": 0.0}
    pair_net_pnl = {
        f"{Timeframe.H1.value}+{Timeframe.M15.value}": 0.0,
        f"{Timeframe.H1.value}+{Timeframe.M5.value}": 0.0,
        f"{Timeframe.M15.value}+{Timeframe.M5.value}": 0.0,
        f"{Timeframe.H1.value}-pivot+{Timeframe.M15.value}": 0.0,
        f"{Timeframe.H1.value}-pivot+{Timeframe.M5.value}": 0.0,
    }
    pairs = _pair_by_authority(run)
    final_reasons: Counter[str] = Counter()
    for attempt, trade in completed:
        side_net_pnl[trade.side.value] += trade.net_pnl
        final_reasons[trade.final_reason] += 1
        pair = pairs.get(attempt.authority_id, "unknown")
        pair_net_pnl[pair] = pair_net_pnl.get(pair, 0.0) + trade.net_pnl

    net_total = sum(net_values)
    return ReplayAnalysis(
        total_trades=total,
        win_rate=(sum(value > 0 for value in net_values) / total if total else 0.0),
        net_pnl_total=net_total,
        net_pnl_mean=(net_total / total if total else 0.0),
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        final_reason_counts=dict(final_reasons),
        side_net_pnl=side_net_pnl,
        confluence_pair_net_pnl=pair_net_pnl,
    )


__all__ = ["ReplayAnalysis", "analyze_historical_replay"]


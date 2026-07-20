from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import ictbt.easychart_v0.portfolio_open_slot as open_slot
from ictbt.easychart_v0.domain import Timeframe


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def authority(name: str, known_at: str):
    return SimpleNamespace(
        authority_id=name,
        symbol="BTCUSDT",
        known_at=ts(known_at),
    )


def opportunity(item):
    return SimpleNamespace(
        opportunity_id=f"opportunity:{item.authority_id}",
        authority=item,
    )


def context(*authorities):
    return SimpleNamespace(
        context_id="trial:2025:BTCUSDT",
        symbol="BTCUSDT",
        book=SimpleNamespace(frames={Timeframe.M15: pd.DataFrame()}),
        candles=pd.DataFrame(),
        scored_authorities=tuple(authorities),
        score_start=ts("2025-01-01 00:00"),
        score_end=ts("2025-01-02 00:00"),
        data_end=ts("2025-01-03 00:00"),
        operating_dates=frozenset({ts("2025-01-01 00:00")}),
    )


def trade(name: str, *, entered: str, closed: str, net_pnl: float):
    return SimpleNamespace(
        name=name,
        entry_time=ts(entered),
        closed_at=ts(closed),
        net_pnl=net_pnl,
    )


def replay(name: str, *, entered: str, closed: str, net_pnl: float):
    return SimpleNamespace(
        status="CLOSED",
        trade=trade(name, entered=entered, closed=closed, net_pnl=net_pnl),
        open_position=None,
        events=(),
        rejection_reason=None,
    )


def install_replay_stubs(monkeypatch, schedules, *, quantity: float = 1.0) -> None:
    def fake_intent(candidate, **_kwargs):
        name = candidate.authority.authority_id
        return SimpleNamespace(
            order_id=name,
            quantity=quantity,
            entry_reference=100.0,
            risk_budget=300.0,
        )

    monkeypatch.setattr(open_slot, "intent_from_opportunity", fake_intent)
    monkeypatch.setattr(
        open_slot,
        "replay_intent",
        lambda intent, **_kwargs: schedules[intent.order_id],
    )
    monkeypatch.setattr(
        open_slot,
        "_first_expiration",
        lambda *_args, **_kwargs: None,
    )


def assemble(_book, item, _costs):
    return opportunity(item)


def priority(candidate, _context, _costs):
    return (candidate.authority.authority_id,)


def test_later_candidate_can_win_when_it_fills_before_older_pending_order(
    monkeypatch,
) -> None:
    slow = authority("slow", "2025-01-01 00:00")
    fast = authority("fast", "2025-01-01 00:30")
    install_replay_stubs(
        monkeypatch,
        {
            "slow": replay(
                "slow",
                entered="2025-01-01 02:00",
                closed="2025-01-01 03:00",
                net_pnl=100.0,
            ),
            "fast": replay(
                "fast",
                entered="2025-01-01 01:00",
                closed="2025-01-01 01:30",
                net_pnl=200.0,
            ),
        },
    )

    result = open_slot.run_open_slot_portfolio(
        (context(slow, fast),),
        initial_equity=10_000.0,
        costs=SimpleNamespace(),
        risk=SimpleNamespace(),
        assemble_candidate=assemble,
        candidate_priority=priority,
    )

    assert result.trades == 1
    assert result.closed_attempts[0].authority.authority_id == "fast"
    assert result.final_equity == 10_200.0
    assert result.pending_orders_created == 2
    assert result.maximum_concurrent_pending == 2
    assert result.cross_cancelled_pending == 1


def test_authority_born_while_position_is_open_is_suppressed(monkeypatch) -> None:
    first = authority("first", "2025-01-01 00:00")
    during_open = authority("during-open", "2025-01-01 01:30")
    install_replay_stubs(
        monkeypatch,
        {
            "first": replay(
                "first",
                entered="2025-01-01 01:00",
                closed="2025-01-01 02:00",
                net_pnl=100.0,
            ),
            "during-open": replay(
                "during-open",
                entered="2025-01-01 03:00",
                closed="2025-01-01 04:00",
                net_pnl=100.0,
            ),
        },
    )

    result = open_slot.run_open_slot_portfolio(
        (context(first, during_open),),
        initial_equity=10_000.0,
        costs=SimpleNamespace(),
        risk=SimpleNamespace(),
        assemble_candidate=assemble,
        candidate_priority=priority,
    )

    assert result.trades == 1
    assert result.closed_attempts[0].authority.authority_id == "first"
    assert result.slot_suppressed_authorities == 1


def test_portfolio_wide_notional_cap_applies_before_pending_admission(
    monkeypatch,
) -> None:
    oversized = authority("oversized", "2025-01-01 00:00")
    install_replay_stubs(
        monkeypatch,
        {
            "oversized": replay(
                "oversized",
                entered="2025-01-01 01:00",
                closed="2025-01-01 02:00",
                net_pnl=100.0,
            )
        },
        quantity=1_000.0,
    )

    result = open_slot.run_open_slot_portfolio(
        (context(oversized),),
        initial_equity=10_000.0,
        costs=SimpleNamespace(),
        risk=SimpleNamespace(),
        assemble_candidate=assemble,
        maximum_notional_to_equity=8.0,
        candidate_priority=priority,
    )

    assert result.trades == 0
    assert result.exposure_rejections == 1
    assert result.pending_orders_created == 0

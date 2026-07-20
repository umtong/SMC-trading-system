from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import ictbt.easychart_v0.portfolio as portfolio
from ictbt.easychart_v0.domain import (
    EntryMode,
    OBCausalState,
    PriceZone,
    Side,
    TargetCandidate,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig, RiskConfig
from ictbt.easychart_v0.pipeline import FeatureBook, Opportunity, PlannedEntry


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def book(symbol: str) -> FeatureBook:
    return FeatureBook(
        symbol=symbol,
        tick_size=0.1,
        frames={timeframe: empty_frame() for timeframe in Timeframe},
        order_blocks={timeframe: () for timeframe in Timeframe},
        pivots={timeframe: () for timeframe in Timeframe},
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def authority(symbol: str, authority_id: str, known_at: str):
    return SimpleNamespace(
        symbol=symbol,
        authority_id=authority_id,
        known_at=ts(known_at),
        side=Side.LONG,
        zone=PriceZone(100.0, 100.2),
        has_literal_body_overlap=True,
    )


def opportunity(item) -> Opportunity:
    return Opportunity(
        opportunity_id=f"opportunity:{item.authority_id}",
        symbol=item.symbol,
        side=Side.LONG,
        authority=item,
        planned_entry=PlannedEntry(
            price=100.2,
            available_at=item.known_at,
            mode=EntryMode.LIMIT_FIRST_REVISIT,
            ob_causal_state=OBCausalState.EVENT_CREATED,
        ),
        initial_stop=99.0,
        target=TargetCandidate(
            candidate_id=f"target:{item.authority_id}",
            symbol=item.symbol,
            trade_side=Side.LONG,
            kind="pivot",
            zone=PriceZone(103.0, 103.0),
            known_at=item.known_at,
            source_id=f"pivot:{item.authority_id}",
        ),
        known_at=item.known_at,
    )


def context(symbol: str, context_id: str, authorities: tuple[object, ...]):
    return portfolio.PortfolioContext(
        context_id=context_id,
        symbol=symbol,
        candles=empty_frame(),
        book=book(symbol),
        authorities=authorities,
        score_start=ts("2025-01-01"),
        score_end=ts("2025-01-29"),
        data_end=ts("2025-02-05"),
    )


def costs() -> CostConfig:
    return CostConfig(0.0, 0.0, 0.0, 0.0)


def test_global_portfolio_enforces_one_slot_and_deduplicates_operating_days(
    monkeypatch,
) -> None:
    first = authority("BTCUSDT", "v08-first", "2025-01-02 00:00")
    simultaneous = authority("ETHUSDT", "leader-same-time", "2025-01-02 00:00")
    blocked = authority("ETHUSDT", "v08-blocked", "2025-01-02 00:30")
    contexts = (
        context("BTCUSDT", "btc", (first,)),
        context("ETHUSDT", "eth", (simultaneous, blocked)),
    )

    monkeypatch.setattr(
        portfolio,
        "intent_from_opportunity",
        lambda selected, **kwargs: SimpleNamespace(
            risk_budget=300.0,
            source_id=selected.authority_id,
        ),
    )
    monkeypatch.setattr(portfolio, "_preentry_expiration", lambda *args, **kwargs: None)

    def fake_replay(intent, **kwargs):
        trade = SimpleNamespace(
            entry_time=ts("2025-01-02 00:05"),
            closed_at=ts("2025-01-02 01:00"),
            net_pnl=150.0,
        )
        return SimpleNamespace(
            trade=trade,
            open_position=None,
            status="CLOSED",
            events=(),
        )

    monkeypatch.setattr(portfolio, "replay_intent", fake_replay)

    result = portfolio.run_global_portfolio(
        contexts,
        initial_equity=10_000,
        costs=costs(),
        risk=RiskConfig(),
        assemble_candidate=lambda _book, item, _costs: opportunity(item),
    )

    assert result.trades == 1
    assert result.closed_attempts[0].authority.authority_id == "v08-first"
    assert result.final_equity == 10_150
    assert result.slot_suppressed_authorities == 1
    assert result.simultaneous_candidate_cutoffs == 1
    assert result.simultaneous_candidates == 2
    assert result.operating_days == 28
    assert result.trades_per_operating_day == 1 / 28


def test_score_window_cancels_fill_at_or_after_boundary(monkeypatch) -> None:
    item = authority("BTCUSDT", "v08-late-fill", "2025-01-28 23:55")
    selected_context = context("BTCUSDT", "btc", (item,))
    monkeypatch.setattr(
        portfolio,
        "intent_from_opportunity",
        lambda selected, **kwargs: SimpleNamespace(
            risk_budget=300.0,
            source_id=selected.authority_id,
        ),
    )
    monkeypatch.setattr(portfolio, "_preentry_expiration", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        portfolio,
        "replay_intent",
        lambda *args, **kwargs: SimpleNamespace(
            trade=SimpleNamespace(
                entry_time=ts("2025-01-29 00:00"),
                closed_at=ts("2025-01-29 01:00"),
                net_pnl=500.0,
            ),
            open_position=None,
            status="CLOSED",
            events=(),
        ),
    )

    result = portfolio.run_global_portfolio(
        (selected_context,),
        initial_equity=10_000,
        costs=costs(),
        risk=RiskConfig(),
        assemble_candidate=lambda _book, value, _costs: opportunity(value),
    )

    assert result.trades == 0
    assert result.pending_cancellations == 1
    assert result.final_equity == 10_000


def test_unresolved_position_invalidates_trial_without_forced_close(monkeypatch) -> None:
    item = authority("BTCUSDT", "v08-open", "2025-01-10 00:00")
    selected_context = context("BTCUSDT", "btc", (item,))
    monkeypatch.setattr(
        portfolio,
        "intent_from_opportunity",
        lambda selected, **kwargs: SimpleNamespace(
            risk_budget=300.0,
            source_id=selected.authority_id,
        ),
    )
    monkeypatch.setattr(portfolio, "_preentry_expiration", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        portfolio,
        "replay_intent",
        lambda *args, **kwargs: SimpleNamespace(
            trade=None,
            open_position=SimpleNamespace(filled_at=ts("2025-01-10 00:05")),
            status="OPEN_CENSORED",
            events=(),
        ),
    )

    result = portfolio.run_global_portfolio(
        (selected_context,),
        initial_equity=10_000,
        costs=costs(),
        risk=RiskConfig(),
        assemble_candidate=lambda _book, value, _costs: opportunity(value),
    )

    assert not result.valid
    assert result.open_censored == 1
    assert result.final_equity == 10_000

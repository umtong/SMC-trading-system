from __future__ import annotations

import pandas as pd

from ictbt.easychart_v0.domain import (
    FairValueGap,
    FormationBar,
    PriceZone,
    Side,
    StrictPivot,
    Timeframe,
)
from ictbt.easychart_v0.liquidity_destination import (
    find_intervening_structure,
    select_pivot_owned_destination,
)
from ictbt.easychart_v0.pipeline import FeatureBook


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def formation_bar(opened: str, *, low: float, high: float) -> FormationBar:
    start = ts(opened)
    return FormationBar(
        open_time=start,
        close_time=start + pd.Timedelta(minutes=5),
        open=(low + high) / 2,
        high=high,
        low=low,
        close=(low + high) / 2,
        volume=100.0,
    )


def opposing_gap(zone: PriceZone) -> FairValueGap:
    bars = (
        formation_bar("2025-01-01 00:00", low=104.0, high=105.0),
        formation_bar("2025-01-01 00:05", low=105.0, high=106.0),
        formation_bar("2025-01-01 00:10", low=106.0, high=107.0),
    )
    return FairValueGap(
        fvg_id="opposing-gap",
        symbol="BTCUSDT",
        timeframe=Timeframe.M15,
        side=Side.SHORT,
        formation_bars=bars,
        zone=zone,
        known_at=bars[-1].close_time,
    )


def pivot(price: float) -> StrictPivot:
    return StrictPivot(
        pivot_id="external-h4-high",
        symbol="BTCUSDT",
        timeframe=Timeframe.H4,
        kind="high",
        price=price,
        pivot_time=ts("2024-12-31 12:00"),
        known_at=ts("2024-12-31 20:00"),
    )


def book(*, gap: FairValueGap | None, target_price: float | None = 110.0) -> FeatureBook:
    pivots = {timeframe: () for timeframe in Timeframe}
    if target_price is not None:
        pivots[Timeframe.H4] = (pivot(target_price),)
    fvgs = {timeframe: () for timeframe in Timeframe}
    if gap is not None:
        fvgs[Timeframe.M15] = (gap,)
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames={timeframe: empty_frame() for timeframe in Timeframe},
        order_blocks={timeframe: () for timeframe in Timeframe},
        pivots=pivots,
        fvgs=fvgs,
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def decision(feature_book: FeatureBook):
    return select_pivot_owned_destination(
        feature_book,
        side=Side.LONG,
        entry_price=100.0,
        target_known_by=ts("2025-01-01 00:30"),
        decision_at=ts("2025-01-01 01:00"),
        target_timeframes=(Timeframe.H1, Timeframe.H4),
    )


def test_nearer_opposing_fvg_blocks_farther_pivot_target() -> None:
    selected = decision(book(gap=opposing_gap(PriceZone(105.0, 106.0))))

    assert not selected.accepted
    assert selected.reason == "intervening_structure"
    assert selected.target is not None
    assert selected.target.source_id == "external-h4-high"
    assert selected.blocker is not None
    assert selected.blocker.kind == "fvg"
    assert selected.blocker.source_id == "opposing-gap"


def test_same_location_structure_is_confluence_not_a_skipped_obstacle() -> None:
    selected = decision(book(gap=opposing_gap(PriceZone(109.95, 110.05))))

    assert selected.accepted
    assert selected.target is not None
    assert selected.blocker is None


def test_missing_preexisting_pivot_is_rejected() -> None:
    selected = decision(book(gap=None, target_price=None))

    assert not selected.accepted
    assert selected.reason == "no_preexisting_pivot_liquidity"
    assert selected.target is None


def test_existing_target_validator_uses_the_same_first_obstacle_rule() -> None:
    feature_book = book(gap=opposing_gap(PriceZone(105.0, 106.0)))
    selected = decision(book(gap=None))
    assert selected.target is not None

    blocker = find_intervening_structure(
        feature_book,
        side=Side.LONG,
        entry_price=100.0,
        target=selected.target,
        decision_at=ts("2025-01-01 01:00"),
    )

    assert blocker is not None
    assert blocker.source_id == "opposing-gap"

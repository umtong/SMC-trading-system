from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import ictbt.easychart_v0.v08_intraday as intraday
from ictbt.easychart_v0.domain import (
    B1Subtype,
    FormationBar,
    LiquidityEvent,
    ObKind,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig
from ictbt.easychart_v0.pipeline import FeatureBook


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def bar(
    opened: str,
    minutes: int,
    o: float,
    high: float,
    low: float,
    close: float,
) -> FormationBar:
    start = ts(opened)
    return FormationBar(
        open_time=start,
        close_time=start + pd.Timedelta(minutes=minutes),
        open=o,
        high=high,
        low=low,
        close=close,
        volume=100.0,
    )


def frame(items: tuple[FormationBar, ...]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
            }
            for item in items
        ],
        index=pd.DatetimeIndex(
            [item.open_time for item in items],
            name="open_time",
        ),
        dtype=float,
    )


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def location() -> OrderBlock:
    first = bar("2025-01-01 00:00", 15, 101.0, 101.5, 99.0, 100.0)
    second = bar("2025-01-01 00:15", 15, 100.0, 102.0, 99.5, 101.4)
    return OrderBlock(
        ob_id="m15-location",
        symbol="BTCUSDT",
        timeframe=Timeframe.M15,
        kind=ObKind.SIMPLE_2C,
        side=Side.LONG,
        formation_bars=(first, second),
        zone=PriceZone(100.0, 101.0),
        known_at=second.close_time,
        stop_extreme=99.0,
        initial_stop=98.9,
        impulse_extreme=102.0,
    )


def pivot(
    pivot_id: str,
    timeframe: Timeframe,
    kind: str,
    price: float,
    pivot_time: str,
    known_at: str,
) -> StrictPivot:
    return StrictPivot(
        pivot_id=pivot_id,
        symbol="BTCUSDT",
        timeframe=timeframe,
        kind=kind,  # type: ignore[arg-type]
        price=price,
        pivot_time=ts(pivot_time),
        known_at=ts(known_at),
    )


def costs() -> CostConfig:
    return CostConfig(0.0002, 0.0006, 0.0002, 0.0006, 2.0, 2.0)


def book_with_internal_sweep() -> FeatureBook:
    bars = (
        bar("2025-01-01 00:30", 5, 100.8, 101.0, 100.5, 100.8),
        bar("2025-01-01 00:35", 5, 100.8, 101.1, 100.6, 100.9),
        bar("2025-01-01 00:40", 5, 100.9, 101.0, 99.8, 100.6),
    )
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    frames[Timeframe.M5] = frame(bars)
    pivots = {timeframe: () for timeframe in Timeframe}
    pivots[Timeframe.M5] = (
        pivot(
            "internal-low",
            Timeframe.M5,
            "low",
            100.0,
            "2025-01-01 00:25",
            "2025-01-01 00:35",
        ),
    )
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames=frames,
        order_blocks={
            timeframe: ((location(),) if timeframe is Timeframe.M15 else ())
            for timeframe in Timeframe
        },
        pivots=pivots,
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def test_detector_uses_internal_m5_liquidity_inside_m15_location(monkeypatch) -> None:
    monkeypatch.setattr(intraday, "_order_block_is_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        intraday,
        "_structure_location_side_is_allowed",
        lambda *args, **kwargs: True,
    )

    sweeps, pairs, rejections = intraday._detect_internal_m5_sweeps(
        book_with_internal_sweep()
    )

    assert pairs == 1
    assert rejections == 0
    assert len(sweeps) == 1
    assert sweeps[0].pivot.timeframe is Timeframe.M5
    assert sweeps[0].event.event_extreme == 99.8
    assert sweeps[0].event.subtype is B1Subtype.SWEEP_RECLAIM


def event(event_id: str, opened: str, node_price: float) -> LiquidityEvent:
    start = ts(opened)
    return LiquidityEvent(
        event_id=event_id,
        symbol="BTCUSDT",
        timeframe=Timeframe.M5,
        subtype=B1Subtype.SWEEP_RECLAIM,
        side=Side.LONG,
        node_id=f"pivot-{event_id}",
        node_price=node_price,
        event_time=start,
        known_at=start + pd.Timedelta(minutes=5),
        event_extreme=node_price - 0.2,
    )


def delivery_ob(ob_id: str, opened: str) -> OrderBlock:
    first = bar(opened, 5, 100.2, 100.4, 99.8, 100.0)
    second_start = (first.open_time + pd.Timedelta(minutes=5)).strftime(
        "%Y-%m-%d %H:%M"
    )
    second = bar(second_start, 5, 100.0, 101.8, 99.9, 101.5)
    return OrderBlock(
        ob_id=ob_id,
        symbol="BTCUSDT",
        timeframe=Timeframe.M5,
        kind=ObKind.SIMPLE_2C,
        side=Side.LONG,
        formation_bars=(first, second),
        zone=PriceZone(100.0, 100.2),
        known_at=second.close_time,
        stop_extreme=99.8,
        initial_stop=99.7,
        impulse_extreme=101.8,
    )


def minimal_book(
    *,
    blocks: tuple[OrderBlock, ...],
) -> FeatureBook:
    frames = {timeframe: empty_frame() for timeframe in Timeframe}
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.1,
        frames=frames,
        order_blocks={
            timeframe: (
                (location(),)
                if timeframe is Timeframe.M15
                else blocks
                if timeframe is Timeframe.M5
                else ()
            )
            for timeframe in Timeframe
        },
        pivots={timeframe: () for timeframe in Timeframe},
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={Timeframe.M5: (), Timeframe.M15: ()},
    )


def candidate(
    *,
    root: str,
    block: OrderBlock,
    displacement_pivot: StrictPivot,
    stop: float,
):
    return SimpleNamespace(
        known_at=block.known_at,
        kind="ob",
        delivery_root_id=root,
        pivot=displacement_pivot,
        order_block=block,
        fvg=None,
        zone=block.zone,
        entry_zone_source="ob_body",
        stop_owner="m15_event",
        stop_extreme=stop + 0.1,
        initial_stop=stop,
        impulse_extreme=block.impulse_extreme,
    )


def test_builder_rearms_distinct_internal_sweeps_and_keeps_pivot_targets(
    monkeypatch,
) -> None:
    first_event = event("event-1", "2025-01-01 01:00", 100.0)
    second_event = event("event-2", "2025-01-01 02:00", 100.3)
    first_block = delivery_ob("delivery-1", "2025-01-01 01:05")
    second_block = delivery_ob("delivery-2", "2025-01-01 02:05")
    displacement = pivot(
        "m5-break-high",
        Timeframe.M5,
        "high",
        101.0,
        "2025-01-01 00:30",
        "2025-01-01 00:45",
    )
    candidates = {
        first_block.ob_id: candidate(
            root="root-1",
            block=first_block,
            displacement_pivot=displacement,
            stop=99.7,
        ),
        second_block.ob_id: candidate(
            root="root-2",
            block=second_block,
            displacement_pivot=displacement,
            stop=100.0,
        ),
    }
    selected_location = location()
    monkeypatch.setattr(
        intraday,
        "_detect_internal_m5_sweeps",
        lambda _book: (
            (
                intraday._LocationSweep(
                    selected_location,
                    pivot(
                        "internal-1",
                        Timeframe.M5,
                        "low",
                        100.0,
                        "2025-01-01 00:30",
                        "2025-01-01 00:45",
                    ),
                    first_event,
                ),
                intraday._LocationSweep(
                    selected_location,
                    pivot(
                        "internal-2",
                        Timeframe.M5,
                        "low",
                        100.3,
                        "2025-01-01 01:30",
                        "2025-01-01 01:45",
                    ),
                    second_event,
                ),
            ),
            2,
            0,
        ),
    )
    monkeypatch.setattr(
        intraday,
        "_ob_candidate",
        lambda _book, *, event, location, block: (
            candidates.get(block.ob_id)
            if (
                (event.event_id == "event-1" and block.ob_id == "delivery-1")
                or (event.event_id == "event-2" and block.ob_id == "delivery-2")
            )
            else None
        ),
    )
    monkeypatch.setattr(intraday, "_fvg_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(intraday, "_displacement_is_material", lambda *args, **kwargs: True)
    monkeypatch.setattr(intraday, "_order_block_is_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(intraday, "_m5_sweep_episode_is_valid", lambda *args, **kwargs: True)
    monkeypatch.setattr(intraday, "_target_touched", lambda *args, **kwargs: False)
    monkeypatch.setattr(intraday, "_required_notional_to_equity", lambda *args, **kwargs: 2.0)
    monkeypatch.setattr(
        intraday,
        "_external_pivot_destination",
        lambda _book, event, *, entry_reference: TargetCandidate(
            candidate_id=f"target-{event.event_id}",
            symbol="BTCUSDT",
            trade_side=Side.LONG,
            kind="pivot",
            zone=PriceZone(104.0, 104.0),
            known_at=ts("2024-12-31 20:00"),
            source_id=f"external-{event.event_id}",
        ),
    )

    result = intraday.build_v08_intraday_liquidity_delivery_result(
        minimal_book(blocks=(first_block, second_block)),
        costs=costs(),
    )

    assert len(result.authorities) == 2
    assert result.diagnostics.authorities == 2
    assert {item.liquidity_event.event_id for item in result.authorities} == {
        "event-1",
        "event-2",
    }
    assert {item.destination.kind for item in result.authorities} == {"pivot"}
    assert all(item.authority_id.startswith("v08-internal-liquidity-delivery:") for item in result.authorities)


def test_builder_rejects_weak_delivery_or_missing_external_liquidity(monkeypatch) -> None:
    selected_event = event("event-1", "2025-01-01 01:00", 100.0)
    block = delivery_ob("delivery-1", "2025-01-01 01:05")
    displacement = pivot(
        "m5-break-high",
        Timeframe.M5,
        "high",
        101.0,
        "2025-01-01 00:30",
        "2025-01-01 00:45",
    )
    selected_candidate = candidate(
        root="root-1",
        block=block,
        displacement_pivot=displacement,
        stop=99.7,
    )
    monkeypatch.setattr(
        intraday,
        "_detect_internal_m5_sweeps",
        lambda _book: (
            (
                intraday._LocationSweep(
                    location(),
                    pivot(
                        "internal-1",
                        Timeframe.M5,
                        "low",
                        100.0,
                        "2025-01-01 00:30",
                        "2025-01-01 00:45",
                    ),
                    selected_event,
                ),
            ),
            1,
            0,
        ),
    )
    monkeypatch.setattr(
        intraday,
        "_ob_candidate",
        lambda *args, **kwargs: selected_candidate,
    )
    monkeypatch.setattr(intraday, "_fvg_candidate", lambda *args, **kwargs: None)
    monkeypatch.setattr(intraday, "_order_block_is_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(intraday, "_m5_sweep_episode_is_valid", lambda *args, **kwargs: True)

    monkeypatch.setattr(intraday, "_displacement_is_material", lambda *args, **kwargs: False)
    weak = intraday.build_v08_intraday_liquidity_delivery_result(
        minimal_book(blocks=(block,)),
        costs=costs(),
    )
    assert weak.authorities == ()
    assert weak.diagnostics.weak_displacement_rejections == 1

    monkeypatch.setattr(intraday, "_displacement_is_material", lambda *args, **kwargs: True)
    monkeypatch.setattr(intraday, "_external_pivot_destination", lambda *args, **kwargs: None)
    missing = intraday.build_v08_intraday_liquidity_delivery_result(
        minimal_book(blocks=(block,)),
        costs=costs(),
    )
    assert missing.authorities == ()
    assert missing.diagnostics.external_liquidity_missing == 1

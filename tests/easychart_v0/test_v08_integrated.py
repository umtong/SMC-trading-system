from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from ictbt.easychart_v0.domain import Side, Timeframe
from ictbt.easychart_v0.target_ownership import PivotOwnershipReason
from ictbt.easychart_v0 import v08_integrated as module


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def fixture(monkeypatch, *, blocker=None, net_target_r: float = 1.0):
    event = SimpleNamespace(
        known_at=ts("2025-01-01 10:05"),
        event_time=ts("2025-01-01 10:00"),
        event_id="event",
        node_id="internal-pivot",
        side=Side.LONG,
    )
    location = SimpleNamespace(ob_id="m15-location", known_at=ts("2025-01-01"))
    item = SimpleNamespace(event=event, location=location)
    candidate = SimpleNamespace(
        known_at=ts("2025-01-01 10:15"),
        delivery_root_id="root",
        kind="ob",
        pivot=SimpleNamespace(pivot_id="broken-m5-pivot"),
        order_block=SimpleNamespace(ob_id="delivery-ob"),
        fvg=None,
        zone=SimpleNamespace(width=1.0),
        initial_stop=98.0,
    )
    target = SimpleNamespace(source_id="external-h1", order_price=103.0)
    owned = SimpleNamespace(
        candidate=target,
        reason=PivotOwnershipReason.HTF_EXTERNAL,
    )
    accepted = SimpleNamespace(
        known_at=candidate.known_at,
        side=Side.LONG,
        delivery_root_id="root",
        entry_zone_source="ob_body",
        zone=SimpleNamespace(width=1.0),
        liquidity_event=event,
        location_ob=location,
        authority_id="v09-owned",
    )
    source_block = SimpleNamespace(known_at=ts("2025-01-01 10:10"))
    book = SimpleNamespace(
        symbol="BTCUSDT",
        order_blocks={
            Timeframe.M5: (source_block,),
            Timeframe.M15: (object(),),
        },
        fvgs={Timeframe.M5: ()},
    )

    monkeypatch.setattr(
        module,
        "_detect_internal_m5_sweeps",
        lambda _book: ((item,), 1, 0),
    )
    monkeypatch.setattr(module, "_ob_candidate", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(module, "_candidate_key", lambda _candidate: (0,))
    monkeypatch.setattr(module, "_displacement_is_material", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_order_block_is_active", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_m5_sweep_episode_is_valid", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "_entry_price", lambda *_args, **_kwargs: 100.0)
    monkeypatch.setattr(module, "owned_pivot_targets", lambda *_args, **_kwargs: (owned,))
    monkeypatch.setattr(module, "_target_touched", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        module,
        "find_intervening_structure",
        lambda *_args, **_kwargs: blocker,
    )
    monkeypatch.setattr(
        module,
        "cost_inclusive_target_r",
        lambda *_args, **_kwargs: net_target_r,
    )
    monkeypatch.setattr(
        module,
        "_required_notional_to_equity",
        lambda *_args, **_kwargs: 2.0,
    )
    monkeypatch.setattr(module, "_authority", lambda **_kwargs: accepted)
    return book, accepted


def test_integrated_builder_rejects_a_far_target_through_nearer_structure(
    monkeypatch,
) -> None:
    book, _ = fixture(monkeypatch, blocker=object())

    result = module.build_v08_integrated_intraday_result(
        book,
        costs=object(),
    )

    assert result.authorities == ()
    assert result.diagnostics.intervening_structure_rejections == 1


def test_integrated_builder_requires_cost_inclusive_target_room(monkeypatch) -> None:
    book, _ = fixture(monkeypatch, net_target_r=0.64)

    result = module.build_v08_integrated_intraday_result(
        book,
        costs=object(),
    )

    assert result.authorities == ()
    assert result.diagnostics.net_target_space_rejections == 1


def test_integrated_builder_accepts_owned_unblocked_economic_target(monkeypatch) -> None:
    book, accepted = fixture(monkeypatch)

    result = module.build_v08_integrated_intraday_result(
        book,
        costs=object(),
    )

    assert result.authorities == (accepted,)
    assert result.diagnostics.authorities == 1

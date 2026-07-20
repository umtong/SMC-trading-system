from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import ictbt.easychart_v0.v08_intraday_hardened as hardened
from ictbt.easychart_v0.domain import PriceZone, Side, TargetCandidate


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def authority(name: str):
    return SimpleNamespace(
        authority_id=name,
        side=Side.LONG,
        zone=PriceZone(100.0, 100.2),
        known_at=ts("2025-01-01 01:00"),
        destination=TargetCandidate(
            candidate_id=f"target:{name}",
            symbol="BTCUSDT",
            trade_side=Side.LONG,
            kind="pivot",
            zone=PriceZone(104.0, 104.0),
            known_at=ts("2024-12-31 20:00"),
            source_id=f"target-{name}",
        ),
        location_ob=SimpleNamespace(ob_id=f"location-{name}"),
        displacement_pivot=SimpleNamespace(pivot_id=f"displacement-{name}"),
        delivery_ob=None,
        delivery_fvg=None,
        initial_stop=99.0,
    )


def diagnostics():
    return SimpleNamespace(
        m15_locations=5,
        internal_m5_pivot_pairs=4,
        internal_sweep_events=3,
        context_rejections=1,
        episodes_without_prompt_delivery=2,
        weak_displacement_rejections=3,
        external_liquidity_missing=4,
        target_used_before_delivery=5,
        target_space_rejections=6,
        exposure_rejections=7,
        duplicate_scenes_suppressed=8,
        authorities=2,
    )


def test_intraday_hardening_rejects_path_skipping_authority(monkeypatch) -> None:
    accepted = authority("accepted")
    blocked = authority("blocked")
    monkeypatch.setattr(
        hardened,
        "build_v08_intraday_liquidity_delivery_result",
        lambda *args, **kwargs: SimpleNamespace(
            authorities=(accepted, blocked),
            diagnostics=diagnostics(),
        ),
    )
    monkeypatch.setattr(
        hardened,
        "cost_inclusive_target_r",
        lambda *args, **kwargs: 1.0,
    )
    monkeypatch.setattr(
        hardened,
        "find_intervening_structure",
        lambda *args, target, **kwargs: (
            None
            if target.source_id == "target-accepted"
            else TargetCandidate(
                candidate_id="near-obstacle",
                symbol="BTCUSDT",
                trade_side=Side.LONG,
                kind="order_block",
                zone=PriceZone(102.0, 102.5),
                known_at=ts("2025-01-01 00:45"),
                source_id="near-ob",
            )
        ),
    )

    result = hardened.build_v08_intraday_hardened_result(
        SimpleNamespace(),
        costs=SimpleNamespace(),
    )

    assert result.authorities == (accepted,)
    assert result.diagnostics.base_authorities == 2
    assert result.diagnostics.intervening_structure_rejections == 1
    assert result.diagnostics.authorities == 1

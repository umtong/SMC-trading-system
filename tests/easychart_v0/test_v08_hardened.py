from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import ictbt.easychart_v0.v08_hardened as hardened
from ictbt.easychart_v0.domain import PriceZone, Side, TargetCandidate, Timeframe
from ictbt.easychart_v0.liquidity_destination import PivotDestinationDecision


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def candidate(timeframe: Timeframe, authority_id: str):
    return SimpleNamespace(
        authority_id=authority_id,
        scene_root_id="shared-scene",
        side=Side.LONG,
        zone=PriceZone(100.0, 100.0),
        initial_stop=99.0,
        known_at=ts("2025-01-01 01:00"),
        boundary_pivot=SimpleNamespace(
            timeframe=timeframe,
            pivot_id=f"{timeframe.value}-boundary",
            pivot_time=ts("2024-12-31 20:00"),
        ),
        break_bar=SimpleNamespace(open_time=ts("2025-01-01 00:30")),
        fvg=SimpleNamespace(fvg_id="execution-fvg"),
        destination=None,
    )


def target(source_id: str = "external-h4-high") -> TargetCandidate:
    return TargetCandidate(
        candidate_id=f"target:{source_id}",
        symbol="BTCUSDT",
        trade_side=Side.LONG,
        kind="pivot",
        zone=PriceZone(104.0, 104.0),
        known_at=ts("2024-12-31 20:00"),
        source_id=source_id,
    )


def fake_replace(authority, **changes):
    values = dict(vars(authority))
    values.update(changes)
    return SimpleNamespace(**values)


def patch_common(monkeypatch, candidates) -> None:
    monkeypatch.setattr(
        hardened,
        "build_v07_scene_family_result",
        lambda _book: SimpleNamespace(authorities=(candidates[0],)),
    )
    monkeypatch.setattr(
        hardened,
        "build_v08_boundary_candidates",
        lambda _book: tuple(candidates),
    )
    monkeypatch.setattr(
        hardened,
        "_displacement_is_material",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        hardened,
        "_required_notional_to_equity",
        lambda *args, **kwargs: 2.0,
    )
    monkeypatch.setattr(
        hardened,
        "_net_target_r",
        lambda *args, **kwargs: 1.5,
    )
    monkeypatch.setattr(hardened, "replace", fake_replace)


def test_h1_candidate_survives_before_legacy_m15_preference(monkeypatch) -> None:
    m15 = candidate(Timeframe.M15, "m15")
    h1 = candidate(Timeframe.H1, "h1")
    patch_common(monkeypatch, (m15, h1))
    monkeypatch.setattr(
        hardened,
        "_context_mode",
        lambda _book, authority, *, policy: (
            "h1_range_expansion"
            if authority.boundary_pivot.timeframe is Timeframe.H1
            else None
        ),
    )
    monkeypatch.setattr(
        hardened,
        "select_pivot_owned_destination",
        lambda *args, **kwargs: PivotDestinationDecision(
            target=target(),
            blocker=None,
            reason=None,
        ),
    )

    result = hardened.build_v08_hardened_scene_family_result(
        SimpleNamespace(),
        costs=SimpleNamespace(),
    )

    assert len(result.authorities) == 1
    assert result.authorities[0].boundary_pivot.timeframe is Timeframe.H1
    assert result.diagnostics.legacy_v07_scenes == 1
    assert result.diagnostics.boundary_candidates == 2
    assert result.diagnostics.htf_context_rejections == 1
    assert result.diagnostics.h1_range_expansion_authorities == 1


def test_nearer_structure_rejects_otherwise_qualified_htf_scene(monkeypatch) -> None:
    h1 = candidate(Timeframe.H1, "h1")
    patch_common(monkeypatch, (h1,))
    monkeypatch.setattr(
        hardened,
        "_context_mode",
        lambda *args, **kwargs: "trend_continuation",
    )
    monkeypatch.setattr(
        hardened,
        "select_pivot_owned_destination",
        lambda *args, **kwargs: PivotDestinationDecision(
            target=target(),
            blocker=TargetCandidate(
                candidate_id="blocker",
                symbol="BTCUSDT",
                trade_side=Side.LONG,
                kind="fvg",
                zone=PriceZone(102.0, 102.5),
                known_at=ts("2025-01-01 00:45"),
                source_id="near-fvg",
            ),
            reason="intervening_structure",
        ),
    )

    result = hardened.build_v08_hardened_scene_family_result(
        SimpleNamespace(),
        costs=SimpleNamespace(),
    )

    assert result.authorities == ()
    assert result.diagnostics.intervening_structure_rejections == 1

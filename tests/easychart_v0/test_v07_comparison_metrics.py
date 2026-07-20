from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "compare_easychart_v07_scene_families_under_test",
    SCRIPTS / "compare_easychart_v07_scene_families.py",
)
assert SPEC is not None and SPEC.loader is not None
comparison = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = comparison
SPEC.loader.exec_module(comparison)


def _row(
    *,
    window_index: int,
    closed_at: str,
    equity_before: float,
    equity_after: float,
    net_pnl: float,
    net_r: float,
) -> dict[str, object]:
    return {
        "window_index": window_index,
        "closed_at": closed_at,
        "equity_before": equity_before,
        "equity_after": equity_after,
        "net_pnl": net_pnl,
        "net_r": net_r,
        "stop_distance_pct": 0.01,
        "target_r": 1.5,
        "partial_enabled": True,
        "partial_1r": False,
        "final_reason": "initial_target",
    }


def test_portfolio_days_remove_only_cross_symbol_date_overlap() -> None:
    overlapping = (comparison.WINDOWS[2], comparison.WINDOWS[3])
    assert comparison._instrument_days(overlapping) == 28
    assert len(comparison._portfolio_operating_dates(overlapping)) == 14

    assert comparison._instrument_days(comparison.WINDOWS) == 84
    assert len(comparison._portfolio_operating_dates(comparison.WINDOWS)) == 70


def test_metrics_use_completed_trade_equity_factors_for_log_growth() -> None:
    rows = [
        _row(
            window_index=0,
            closed_at="2026-01-01T01:00:00+00:00",
            equity_before=100.0,
            equity_after=110.0,
            net_pnl=10.0,
            net_r=1.0,
        ),
        _row(
            window_index=1,
            closed_at="2026-01-01T02:00:00+00:00",
            equity_before=200.0,
            equity_after=180.0,
            net_pnl=-20.0,
            net_r=-1.0,
        ),
    ]
    metrics = comparison._metrics(
        rows,
        initial_equity=100.0,
        instrument_days=10,
        portfolio_operating_days=5,
    )

    assert metrics["trades_per_instrument_day"] == pytest.approx(0.2)
    assert metrics["instrument_days"] == 10
    assert metrics["completed_trades_per_portfolio_operating_day"] == pytest.approx(
        0.4
    )
    assert metrics["net_log_growth"] == pytest.approx(math.log(1.1 * 0.9))
    assert metrics["net_log_growth_per_portfolio_operating_day"] == pytest.approx(
        math.log(1.1 * 0.9) / 5
    )
    assert metrics["net_log_growth_per_instrument_day"] == pytest.approx(
        math.log(1.1 * 0.9) / 10
    )


def test_log_growth_compounds_each_reset_window_in_log_space() -> None:
    rows = [
        _row(
            window_index=0,
            closed_at="2026-01-01T01:00:00+00:00",
            equity_before=100.0,
            equity_after=110.0,
            net_pnl=10.0,
            net_r=1.0,
        ),
        _row(
            window_index=0,
            closed_at="2026-01-02T01:00:00+00:00",
            equity_before=110.0,
            equity_after=121.0,
            net_pnl=11.0,
            net_r=1.0,
        ),
        _row(
            window_index=1,
            closed_at="2026-01-02T02:00:00+00:00",
            equity_before=200.0,
            equity_after=220.0,
            net_pnl=20.0,
            net_r=1.0,
        ),
    ]
    assert comparison._net_log_growth(rows) == pytest.approx(
        math.log(1.21) + math.log(1.1)
    )


def test_empty_completed_ledger_has_zero_growth_and_frequency() -> None:
    metrics = comparison._metrics(
        [],
        initial_equity=10_000.0,
        instrument_days=84,
        portfolio_operating_days=70,
    )
    assert metrics["trades"] == 0
    assert metrics["completed_trades_per_portfolio_operating_day"] == 0
    assert metrics["net_log_growth"] == 0
    assert metrics["net_log_growth_per_portfolio_operating_day"] == 0


def test_log_growth_rejects_non_positive_equity() -> None:
    row = _row(
        window_index=0,
        closed_at="2026-01-01T01:00:00+00:00",
        equity_before=100.0,
        equity_after=0.0,
        net_pnl=-100.0,
        net_r=-1.0,
    )
    with pytest.raises(ValueError, match="positive equity"):
        comparison._net_log_growth([row])


def test_global_drawdown_uses_one_chronological_shared_equity_path() -> None:
    rows = [
        _row(
            window_index=0,
            closed_at="2026-01-01T01:00:00+00:00",
            equity_before=100.0,
            equity_after=120.0,
            net_pnl=20.0,
            net_r=1.0,
        ),
        _row(
            window_index=1,
            closed_at="2026-01-01T02:00:00+00:00",
            equity_before=120.0,
            equity_after=90.0,
            net_pnl=-30.0,
            net_r=-1.0,
        ),
    ]

    global_metrics = comparison._metrics(
        rows,
        initial_equity=100.0,
        instrument_days=2,
        portfolio_operating_days=1,
        equity_scope="global_portfolio",
    )
    panel_metrics = comparison._metrics(
        rows,
        initial_equity=100.0,
        instrument_days=2,
        portfolio_operating_days=1,
        equity_scope="window_panel",
    )

    assert global_metrics["max_drawdown_fraction"] == pytest.approx(0.25)
    assert panel_metrics["max_drawdown_fraction"] == pytest.approx(0.10)
    assert global_metrics["cumulative_return"] == pytest.approx(-0.10)

    subset_metrics = comparison._metrics(
        rows[:1],
        initial_equity=100.0,
        instrument_days=1,
        portfolio_operating_days=1,
        equity_scope="global_portfolio",
        include_drawdown=False,
    )
    assert subset_metrics["max_drawdown_fraction"] is None


def test_global_runner_shares_equity_and_position_slot_across_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = comparison.pd.Timestamp("2026-01-01T00:00:00Z")
    family = comparison.SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST

    def authority(name: str, minutes: int) -> SimpleNamespace:
        return SimpleNamespace(
            authority_id=name,
            known_at=t0 + comparison.pd.Timedelta(minutes=minutes),
            scene_family=family,
            has_literal_body_overlap=True,
            zone=SimpleNamespace(width=1.0),
        )

    first = authority("first", 0)
    suppressed = authority("suppressed", 5)
    later = authority("later", 20)
    contexts = (
        SimpleNamespace(
            index=0,
            symbol="BTCUSDT",
            environment="one",
            leader=(first,),
            v07=(),
            candles=object(),
            book=SimpleNamespace(
                frames={comparison.Timeframe.M15: object()}
            ),
            end=t0 + comparison.pd.Timedelta(days=1),
        ),
        SimpleNamespace(
            index=1,
            symbol="ETHUSDT",
            environment="two",
            leader=(suppressed, later),
            v07=(),
            candles=object(),
            book=SimpleNamespace(
                frames={comparison.Timeframe.M15: object()}
            ),
            end=t0 + comparison.pd.Timedelta(days=1),
        ),
    )

    monkeypatch.setattr(
        comparison,
        "_assemble_global_candidate",
        lambda context, item, **kwargs: SimpleNamespace(
            authority=item,
            authority_id=item.authority_id,
            scene_family=item.scene_family,
            known_at=item.known_at,
        ),
    )
    observed_equities: list[float] = []

    def fake_intent(opportunity: SimpleNamespace, *, equity: float, **kwargs: object) -> SimpleNamespace:
        observed_equities.append(equity)
        return SimpleNamespace(authority_id=opportunity.authority_id)

    monkeypatch.setattr(comparison, "intent_from_opportunity", fake_intent)

    def fake_replay(intent: SimpleNamespace, **kwargs: object) -> SimpleNamespace:
        if intent.authority_id == "first":
            trade = SimpleNamespace(
                net_pnl=10.0,
                closed_at=t0 + comparison.pd.Timedelta(minutes=10),
            )
        else:
            trade = SimpleNamespace(
                net_pnl=5.0,
                closed_at=t0 + comparison.pd.Timedelta(minutes=25),
            )
        return SimpleNamespace(status="CLOSED", trade=trade, events=())

    monkeypatch.setattr(comparison, "replay_intent", fake_replay)
    monkeypatch.setattr(
        comparison,
        "_preentry_expiration",
        lambda *args, **kwargs: None,
    )

    result = comparison._run_global_arm(
        contexts,
        arm="synthetic",
        authority_scope="leader",
        execution_arm=comparison.FIRST_RETURN_LIMIT,
        initial_equity=100.0,
        costs=object(),
        risk=object(),
        assemble_v07_opportunity=lambda *args, **kwargs: None,
    )

    assert observed_equities == [100.0, 110.0]
    assert result.final_equity == pytest.approx(115.0)
    assert len(result.closed_attempts) == 2
    assert result.slot_suppressed_authorities == 1


def test_global_next_open_geometry_rejection_falls_back_at_same_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t0 = comparison.pd.Timestamp("2026-01-01T00:00:00Z")

    def authority(name: str) -> SimpleNamespace:
        return SimpleNamespace(
            authority_id=name,
            known_at=t0,
            scene_family=comparison.SceneFamily.SR_FLIP_FVG,
            has_literal_body_overlap=False,
            zone=SimpleNamespace(width=0.0),
        )

    invalid = authority("a-invalid-next-open")
    valid = authority("z-valid-next-open")
    context = SimpleNamespace(
        index=0,
        symbol="BTCUSDT",
        environment="synthetic",
        leader=(),
        v07=(invalid, valid),
        candles=object(),
        book=SimpleNamespace(frames={comparison.Timeframe.M15: object()}),
        end=t0 + comparison.pd.Timedelta(days=1),
    )

    monkeypatch.setattr(
        comparison,
        "_assemble_global_candidate",
        lambda context, item, **kwargs: SimpleNamespace(
            authority=item,
            authority_id=item.authority_id,
            scene_family=item.scene_family,
            known_at=item.known_at,
        ),
    )
    monkeypatch.setattr(
        comparison,
        "_v07_next_open_is_executable",
        lambda context, opportunity, **kwargs: (
            opportunity.authority.authority_id == valid.authority_id
        ),
    )
    monkeypatch.setattr(
        comparison,
        "intent_from_opportunity",
        lambda opportunity, **kwargs: SimpleNamespace(
            authority_id=opportunity.authority_id
        ),
    )
    monkeypatch.setattr(
        comparison,
        "replay_intent",
        lambda intent, **kwargs: SimpleNamespace(
            status="CLOSED",
            trade=SimpleNamespace(
                net_pnl=5.0,
                closed_at=t0 + comparison.pd.Timedelta(minutes=5),
            ),
            events=(),
        ),
    )
    monkeypatch.setattr(
        comparison,
        "_preentry_expiration",
        lambda *args, **kwargs: None,
    )

    result = comparison._run_global_arm(
        (context,),
        arm="synthetic",
        authority_scope="v07",
        execution_arm=comparison.BOUNDARY_ACCEPT_NEXT_OPEN,
        initial_equity=100.0,
        costs=object(),
        risk=object(),
        assemble_v07_opportunity=lambda *args, **kwargs: None,
    )

    assert len(result.closed_attempts) == 1
    assert result.closed_attempts[0].authority.authority_id == valid.authority_id
    assert result.opportunity_rejections == 1


def test_trade_source_classification_uses_sr_flip_fvg_scene_family() -> None:
    timestamp = comparison.pd.Timestamp("2026-01-01T00:00:00Z")
    trade = SimpleNamespace(
        scene_family=comparison.SceneFamily.SR_FLIP_FVG,
        side=SimpleNamespace(value="long"),
        entry_time=timestamp,
        closed_at=timestamp,
        entry_price=100.0,
        initial_stop=99.0,
        initial_target=102.0,
        original_quantity=0.5,
        target_r=2.0,
        exit_legs=(),
        final_reason="initial_target",
        net_pnl=6.0,
    )
    attempt = SimpleNamespace(
        result=SimpleNamespace(trade=trade),
        intent=SimpleNamespace(
            entry_mode=SimpleNamespace(value="limit_first_revisit"),
            created_at=timestamp,
            risk_budget=3.0,
            quantity=1.0,
        ),
        authority_id="authority",
        equity_before=100.0,
        equity_after=106.0,
    )
    authority = SimpleNamespace(
        event_kind="synthetic",
        scene_root_id="root",
        location_id="location",
        boundary_pivot=SimpleNamespace(
            timeframe=comparison.Timeframe.H1,
            price=99.75,
        ),
        fvg=SimpleNamespace(fvg_id="fvg-1"),
        destination=SimpleNamespace(kind="pivot", source_id="pivot-1"),
    )
    candles = comparison.pd.DataFrame(
        {"high": [101.0], "low": [99.5]},
        index=comparison.pd.DatetimeIndex([timestamp]),
    )

    v07_row = comparison._trade_row(
        arm="v07",
        scope="global_portfolio",
        entry_arm=comparison.FIRST_RETURN_LIMIT,
        window_index=0,
        symbol="BTCUSDT",
        environment="synthetic",
        candles=candles,
        attempt=attempt,
        authority=authority,
    )
    trade.scene_family = (
        comparison.SceneFamily.PREEXISTING_OB_STRUCTURE_FIRST_RETEST
    )
    leader_row = comparison._trade_row(
        arm="leader",
        scope="global_portfolio",
        entry_arm="leader_locked_limit",
        window_index=0,
        symbol="BTCUSDT",
        environment="synthetic",
        candles=candles,
        attempt=attempt,
        authority=authority,
    )

    assert v07_row["source_strategy"] == "v07"
    assert v07_row["position_notional"] == pytest.approx(50.0)
    assert v07_row["notional_to_equity"] == pytest.approx(0.5)
    assert v07_row["boundary_timeframe"] == "1h"
    assert v07_row["boundary_price"] == pytest.approx(99.75)
    assert v07_row["fvg_id"] == "fvg-1"
    assert v07_row["target_kind"] == "pivot"
    assert v07_row["target_source_id"] == "pivot-1"
    assert leader_row["source_strategy"] == "leader"
    assert leader_row["boundary_timeframe"] is None

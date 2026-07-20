from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

import ictbt.easychart_v0.application as application
from ictbt.easychart_v0.application import (
    DailyLossGuard,
    load_5m_csv,
    plan_snapshot,
    run_historical_replay,
)
from ictbt.easychart_v0.cli import main
from ictbt.easychart_v0.domain import (
    B1Subtype,
    EntryMode,
    FormationBar,
    LiquidityEvent,
    ObKind,
    OrderBlock,
    PriceZone,
    Side,
    StrictPivot,
    Timeframe,
)
from ictbt.easychart_v0.execution import CostConfig, RiskConfig
from ictbt.easychart_v0.pipeline import (
    FeatureBook,
    Opportunity,
    assemble_confluence_opportunities,
)


def _candles() -> pd.DataFrame:
    index = pd.date_range("2026-01-01T00:00:00Z", periods=60, freq="5min")
    base = pd.Series(range(60), index=index, dtype=float) * 0.1 + 100.0
    return pd.DataFrame(
        {
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base + 0.1,
            "volume": 100.0,
        },
        index=index,
    )


def _write_csv(path) -> None:
    frame = _candles().rename_axis("open_time").reset_index()
    frame.to_csv(path, index=False)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([], tz="UTC", name="open_time"),
        dtype=float,
    )


def _long_block(
    ob_id: str,
    timeframe: Timeframe,
    *,
    known_at: str,
    zone: tuple[float, float],
) -> OrderBlock:
    close_time = pd.Timestamp(known_at)
    delta = {
        Timeframe.M5: pd.Timedelta(minutes=5),
        Timeframe.M15: pd.Timedelta(minutes=15),
        Timeframe.H1: pd.Timedelta(hours=1),
        Timeframe.H4: pd.Timedelta(hours=4),
    }[timeframe]
    first_open = close_time - 2 * delta
    return OrderBlock(
        ob_id=ob_id,
        symbol="BTCUSDT",
        timeframe=timeframe,
        kind=ObKind.SIMPLE_2C,
        side=Side.LONG,
        formation_bars=(
            FormationBar(
                first_open, first_open + delta, 102, 103, 98, 99, 10
            ),
            FormationBar(
                first_open + delta, close_time, 98.5, 106, 97, 104, 20
            ),
        ),
        zone=PriceZone(*zone),
        known_at=close_time,
        stop_extreme=97,
        initial_stop=96.5,
        impulse_extreme=106,
    )


def _replay_book(candles: pd.DataFrame) -> FeatureBook:
    frames = {timeframe: _empty_frame() for timeframe in Timeframe}
    frames[Timeframe.M5] = candles
    h1 = _long_block(
        "h1-location",
        Timeframe.H1,
        known_at="2026-01-01T04:00:00Z",
        zone=(99, 103),
    )
    m15 = _long_block(
        "m15-location",
        Timeframe.M15,
        known_at="2026-01-01T04:15:00Z",
        zone=(100, 102),
    )
    m5 = _long_block(
        "m5-delivery",
        Timeframe.M5,
        known_at="2026-01-01T04:20:00Z",
        zone=(100.5, 101.5),
    )
    order_blocks = {timeframe: () for timeframe in Timeframe}
    order_blocks[Timeframe.H1] = (h1,)
    order_blocks[Timeframe.M15] = (m15,)
    order_blocks[Timeframe.M5] = (m5,)
    pivots = {timeframe: () for timeframe in Timeframe}
    pivots[Timeframe.H1] = tuple(
        StrictPivot(
            pivot_id=pivot_id,
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            kind=kind,
            price=price,
            pivot_time=pd.Timestamp(f"2026-01-01T{hour:02d}:00:00Z"),
            known_at=pd.Timestamp(f"2026-01-01T{hour + 1:02d}:00:00Z"),
        )
        for pivot_id, kind, price, hour in (
            ("high-1", "high", 108, 0),
            ("low-1", "low", 90, 1),
            ("high-2", "high", 112, 2),
            ("low-2", "low", 101, 3),
        )
    )
    pivots[Timeframe.M5] = (
        StrictPivot(
            pivot_id="m5-mss-high",
            symbol="BTCUSDT",
            timeframe=Timeframe.M5,
            kind="high",
            price=102.0,
            pivot_time=pd.Timestamp("2026-01-01T04:05:00Z"),
            known_at=pd.Timestamp("2026-01-01T04:15:00Z"),
        ),
    )
    return FeatureBook(
        symbol="BTCUSDT",
        tick_size=0.5,
        frames=frames,
        order_blocks=order_blocks,
        pivots=pivots,
        fvgs={timeframe: () for timeframe in Timeframe},
        liquidity_events={
            Timeframe.M5: (),
            Timeframe.M15: (
                LiquidityEvent(
                    event_id="event:m15-liquidity",
                    symbol="BTCUSDT",
                    timeframe=Timeframe.M15,
                    subtype=B1Subtype.SWEEP_RECLAIM,
                    side=Side.LONG,
                    node_id="low-2",
                    node_price=101,
                    event_time=m15.formation_bars[-1].open_time,
                    known_at=m15.formation_bars[-1].close_time,
                ),
            ),
        },
    )


def test_mtf_event_created_opportunity_builds_next_open_intent() -> None:
    book = _replay_book(_empty_frame())
    results = assemble_confluence_opportunities(
        book, as_of="2026-01-01T04:20:00Z"
    )
    assert len(results) == 1
    opportunity = results[0]
    assert isinstance(opportunity, Opportunity)

    intent = application.intent_from_opportunity(
        opportunity,
        equity=10_000,
        costs=CostConfig(0.0, 0.0, 0.0, 0.0),
        risk=RiskConfig(risk_fraction=0.01, quantity_step=0.001),
    )

    assert intent.entry_mode.value == "next_bar_open"
    assert intent.ob_causal_state.value == "event_created"

    first_revisit_intent = application.intent_from_opportunity(
        opportunity,
        equity=10_000,
        costs=CostConfig(0.0, 0.0, 0.0, 0.0),
        risk=RiskConfig(risk_fraction=0.01, quantity_step=0.001),
        event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
    )
    assert first_revisit_intent.entry_mode is EntryMode.LIMIT_FIRST_REVISIT


def test_daily_loss_guard_is_inert_when_user_setting_is_off() -> None:
    configured = RiskConfig(
        risk_fraction=0.03,
        daily_loss_limit_enabled=False,
        daily_loss_limit_fraction=0.01,
    )
    guard = DailyLossGuard(configured)

    effective = guard.risk_for_new_order(
        at=pd.Timestamp("2026-01-01T14:00:00Z"),
        equity=10_000,
    )
    assert effective is configured
    assert effective.risk_fraction == 0.03


def test_enabled_daily_loss_guard_shrinks_remaining_risk_then_resets_in_kst() -> None:
    configured = RiskConfig(
        risk_fraction=0.03,
        daily_loss_limit_enabled=True,
        daily_loss_limit_fraction=0.01,
        daily_reset_timezone="Asia/Seoul",
    )
    guard = DailyLossGuard(configured)
    first_time = pd.Timestamp("2026-01-01T14:00:00Z")

    first = guard.risk_for_new_order(at=first_time, equity=10_000)
    assert first.risk_fraction == 0.01

    guard.record_realized(
        closed_at=pd.Timestamp("2026-01-01T14:30:00Z"),
        net_pnl=-60,
        equity_before=10_000,
    )
    after_loss = guard.status(
        at=pd.Timestamp("2026-01-01T14:31:00Z"),
        equity=9_940,
    )
    assert after_loss.realized_net_pnl == -60
    assert after_loss.remaining_loss_budget == 40
    reduced = guard.risk_for_new_order(
        at=pd.Timestamp("2026-01-01T14:31:00Z"),
        equity=9_940,
    )
    assert reduced.risk_fraction == pytest.approx(40 / 9_940)

    guard.record_realized(
        closed_at=pd.Timestamp("2026-01-01T14:45:00Z"),
        net_pnl=-40,
        equity_before=9_940,
    )
    blocked = guard.status(
        at=pd.Timestamp("2026-01-01T14:46:00Z"),
        equity=9_900,
    )
    assert blocked.blocked

    reset = guard.status(
        at=pd.Timestamp("2026-01-01T15:00:00Z"),
        equity=9_900,
    )
    assert reset.local_date.isoformat() == "2026-01-02"
    assert reset.realized_net_pnl == 0
    assert not reset.blocked


def test_load_and_plan_completed_5m_snapshot(tmp_path) -> None:
    source = tmp_path / "BTCUSDT_5m.csv"
    _write_csv(source)

    candles = load_5m_csv(source)
    plan = plan_snapshot(
        candles,
        symbol="BTCUSDT",
        tick_size=0.1,
        equity=10_000.0,
        costs=CostConfig(0.0, 0.0, 0.0, 0.0),
        risk=RiskConfig(risk_fraction=0.01, quantity_step=0.001),
    )

    assert len(plan.book.frames[next(iter(plan.book.frames))]) == 60
    assert plan.as_of == candles.index[-1] + pd.Timedelta(minutes=5)


def test_module_cli_writes_machine_readable_snapshot(tmp_path) -> None:
    source = tmp_path / "BTCUSDT_5m.csv"
    output = tmp_path / "scan.json"
    _write_csv(source)

    status = main(
        [
            "--input",
            str(source),
            "--symbol",
            "BTCUSDT",
            "--tick-size",
            "0.1",
            "--output",
            str(output),
        ]
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert status == 0
    assert payload["engine"] == "easychart_ob_v0"
    assert payload["strategy_version"] == (
        "easychart_ob_v0_3_m15_event_m5_delivery"
    )
    assert payload["mode"] == "snapshot"
    assert payload["features"]["5m"]["candles"] == 60
    assert payload["risk_settings"]["risk_fraction"] == 0.03
    assert payload["risk_settings"]["daily_loss_limit_enabled"] is False
    assert payload["risk_settings"]["daily_reset_timezone"] == "Asia/Seoul"
    assert payload["strategy_settings"]["event_created_entry_mode"] == "next_bar_open"


def test_module_cli_accepts_user_risk_and_daily_limit_overrides(tmp_path) -> None:
    source = tmp_path / "BTCUSDT_5m.csv"
    output = tmp_path / "scan_custom_risk.json"
    _write_csv(source)

    status = main(
        [
            "--input",
            str(source),
            "--symbol",
            "BTCUSDT",
            "--tick-size",
            "0.1",
            "--risk-fraction",
            "0.02",
            "--daily-loss-limit",
            "--daily-loss-fraction",
            "0.015",
            "--daily-reset-timezone",
            "UTC",
            "--event-created-entry-mode",
            "limit_first_revisit",
            "--output",
            str(output),
        ]
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert status == 0
    assert payload["risk_settings"]["risk_fraction"] == 0.02
    assert payload["risk_settings"]["daily_loss_limit_enabled"] is True
    assert payload["risk_settings"]["daily_loss_limit_fraction"] == 0.015
    assert payload["risk_settings"]["daily_reset_timezone"] == "UTC"
    assert (
        payload["strategy_settings"]["event_created_entry_mode"]
        == "limit_first_revisit"
    )


def test_short_csv_replay_runs_confluence_pipeline_and_updates_equity(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "short_5m.csv"
    output = tmp_path / "replay.json"
    frame = pd.DataFrame(
        [
            (100, 103, 99, 102, 100),
            (102, 104, 101, 103, 100),
            (103, 105, 102, 104, 100),
            (101, 120, 100, 112, 100),
            (105, 113, 104, 110, 100),
        ],
        index=pd.date_range("2026-01-01T04:00:00Z", periods=5, freq="5min"),
        columns=["open", "high", "low", "close", "volume"],
    )
    frame.rename_axis("open_time").reset_index().to_csv(source, index=False)
    monkeypatch.setattr(
        application,
        "build_feature_book",
        lambda candles, *, symbol, tick_size: _replay_book(candles),
    )

    status = main(
        [
            "--input",
            str(source),
            "--symbol",
            "BTCUSDT",
            "--tick-size",
            "0.5",
            "--replay",
            "--output",
            str(output),
        ]
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert status == 0
    assert payload["mode"] == "historical_replay"
    assert payload["summary"]["attempts"] == 1
    assert payload["summary"]["closed_trades"] == 1
    assert payload["attempts"][0]["scene"] == "a1_b1_confluence"
    assert payload["attempts"][0]["entry_mode"] == "next_bar_open"
    assert payload["final_equity"] > payload["initial_equity"]
    assert payload["summary"]["expired_before_submission"] == 0
    assert payload["expired_before_submission"] == []


def test_removed_impulse_extreme_does_not_cancel_next_open_entry(monkeypatch) -> None:
    candles = pd.DataFrame(
        [
            (105, 107, 104, 106, 100),
            (105, 106, 101, 103, 100),
        ],
        index=pd.date_range("2026-01-01T04:15:00Z", periods=2, freq="5min"),
        columns=["open", "high", "low", "close", "volume"],
    )
    monkeypatch.setattr(
        application,
        "build_feature_book",
        lambda frame, *, symbol, tick_size: _replay_book(frame),
    )

    run = run_historical_replay(
        candles,
        symbol="BTCUSDT",
        tick_size=0.5,
        equity=10_000,
        costs=CostConfig(0.0, 0.0, 0.0, 0.0),
        risk=RiskConfig(risk_fraction=0.01, quantity_step=0.001),
    )

    assert len(run.attempts) == 1
    assert run.attempts[0].result.status == "OPEN_CENSORED"
    assert run.pending_cancellations == ()


def test_entry_rejection_precedes_a_later_structural_expiration(monkeypatch) -> None:
    rejected_at = pd.Timestamp("2026-01-01T04:20:00Z")
    later_expiration = rejected_at + pd.Timedelta(minutes=5)
    replay = SimpleNamespace(
        status="ENTRY_REJECTED",
        trade=None,
        open_position=None,
        events=(SimpleNamespace(kind="entry_rejected", occurred_at=rejected_at),),
    )
    monkeypatch.setattr(
        application,
        "_first_opportunity_expiration",
        lambda _book, _opportunity: (
            later_expiration,
            "initial_target_used_before_entry",
        ),
    )

    assert application._pending_cancellation(None, None, replay) is None

    earlier_expiration = rejected_at - pd.Timedelta(minutes=5)
    monkeypatch.setattr(
        application,
        "_first_opportunity_expiration",
        lambda _book, _opportunity: (
            earlier_expiration,
            "initial_target_used_before_entry",
        ),
    )
    assert application._pending_cancellation(None, None, replay) == (
        earlier_expiration,
        "initial_target_used_before_entry",
    )


def test_historical_replay_records_expiration_before_late_submission(monkeypatch) -> None:
    candles = _candles()
    book = _replay_book(candles)
    cutoff = pd.Timestamp("2026-01-01T04:20:00Z")
    result = assemble_confluence_opportunities(book, as_of=cutoff)[0]
    assert isinstance(result, Opportunity)

    monkeypatch.setattr(
        application,
        "build_feature_book",
        lambda _frame, *, symbol, tick_size: book,
    )
    monkeypatch.setattr(
        application,
        "_plan_from_book",
        lambda _book, *, cutoff, **_kwargs: SimpleNamespace(
            results=(result,) if cutoff == result.known_at else (),
            sizing_rejections=(),
            candidate_intents=(),
        ),
    )
    monkeypatch.setattr(
        application,
        "_first_opportunity_expiration",
        lambda _book, _opportunity: (
            cutoff,
            "liquidity_event_invalidated_before_entry",
        ),
    )

    run = run_historical_replay(
        candles,
        symbol="BTCUSDT",
        tick_size=0.5,
        equity=10_000,
        costs=CostConfig(0.0, 0.0, 0.0, 0.0),
        risk=RiskConfig(risk_fraction=0.01, quantity_step=0.001),
    )

    assert run.attempts == ()
    assert run.pending_cancellations == ()
    assert len(run.expired_before_submission) == 1
    expiration = run.expired_before_submission[0]
    assert expiration.authority_id == result.authority_id
    assert expiration.expired_at == cutoff
    assert expiration.reason == "liquidity_event_invalidated_before_entry"

def _comparison_module(monkeypatch):
    project_root = Path(__file__).resolve().parents[2]
    scripts = project_root / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    name = "compare_easychart_delivery_entry_arms_test"
    spec = importlib.util.spec_from_file_location(
        name, scripts / "compare_easychart_delivery_entry_arms.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_comparison_global_excludes_expired_candidate_before_tie_break(monkeypatch) -> None:
    comparison = _comparison_module(monkeypatch)
    cutoff = pd.Timestamp("2026-01-01T04:20:00Z")
    stale = SimpleNamespace(authority_id="stale-authority")
    context = SimpleNamespace(
        index=0,
        decision_times=(cutoff,),
        book=object(),
    )
    monkeypatch.setattr(
        comparison,
        "assemble_confluence_opportunities",
        lambda *_args, **_kwargs: (stale,),
    )
    monkeypatch.setattr(
        comparison,
        "_first_opportunity_expiration",
        lambda _book, _opportunity: (
            cutoff,
            "initial_target_used_before_entry",
        ),
    )

    result = comparison._run_global(
        (context,),
        arm=comparison.ARM_FIRST_REVISIT,
        event_created_entry_mode=EntryMode.LIMIT_FIRST_REVISIT,
        initial_equity=10_000,
        costs=CostConfig(0.0, 0.0, 0.0, 0.0),
        risk=RiskConfig(risk_fraction=0.01, quantity_step=0.001),
    )

    assert result.observations == ()
    assert result.expired_before_submission == 1
    assert result.final_equity == 10_000


def test_comparison_tie_break_uses_symbol_before_authority_id(monkeypatch) -> None:
    comparison = _comparison_module(monkeypatch)

    def opportunity(symbol: str, authority_id: str):
        authority = SimpleNamespace(
            location=SimpleNamespace(timeframe=Timeframe.M15),
            confirmation=SimpleNamespace(timeframes=(Timeframe.M5,)),
            has_literal_body_overlap=True,
            zone=SimpleNamespace(width=1.0),
            known_at=pd.Timestamp("2026-01-01T04:20:00Z"),
            authority_id=authority_id,
        )
        return SimpleNamespace(symbol=symbol, authority=authority)

    btc = comparison._authority_priority(opportunity("BTCUSDT", "z-authority"))
    eth = comparison._authority_priority(opportunity("ETHUSDT", "a-authority"))

    assert btc < eth
    assert btc[-2:] == ("BTCUSDT", "z-authority")


def test_comparison_legacy_sums_available_censor_fields(tmp_path, monkeypatch) -> None:
    comparison = _comparison_module(monkeypatch)
    (tmp_path / "summary_0.json").write_text(
        json.dumps(
            {
                "windows": [
                    {
                        "environment": "fixture",
                        "cancellations": 2,
                        "rejections": 3,
                        "open_censored": 4,
                        "entry_censored": 5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        [
            {
                "status": "closed",
                "environment": "fixture",
                "authority_id": "legacy-authority",
                "side": "long",
                "created_at": "2026-01-01T00:00:00Z",
                "entry_time": "2026-01-01T00:05:00Z",
                "closed_at": "2026-01-01T00:10:00Z",
                "entry_price": 100.0,
                "initial_stop": 90.0,
                "initial_target": 110.0,
                "target_r": 1.0,
                "final_reason": "initial_target",
                "net_pnl": 50.0,
            }
        ]
    ).to_csv(tmp_path / "records_0.csv", index=False)

    windows, aggregate, _ledger = comparison._legacy_reference(
        tmp_path,
        indices=(0,),
        initial_equity=10_000,
        risk_fraction=0.01,
    )

    assert windows[0]["open_censored"] == 4
    assert windows[0]["entry_censored"] == 5
    assert aggregate["open_censored"] == 4
    assert aggregate["entry_censored"] == 5


def test_comparison_rejects_nonlegacy_risk_fraction(monkeypatch) -> None:
    comparison = _comparison_module(monkeypatch)
    monkeypatch.setattr(
        comparison,
        "_parse_args",
        lambda: argparse.Namespace(initial_equity=10_000.0, risk_fraction=0.03),
    )

    with pytest.raises(ValueError, match="requires risk-fraction 0.01"):
        comparison.main()

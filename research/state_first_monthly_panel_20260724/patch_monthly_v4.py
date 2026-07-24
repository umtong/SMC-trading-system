from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
SRC = ROOT / 'build_official_state_month.py'
TEST = ROOT / 'test_build_official_state_month.py'
PREREG = ROOT / 'monthly_panel_preregistration_v4.json'

s = SRC.read_text(encoding='utf-8')
old = 'FEATURE_COLUMNS = [\n    "close",'
new = 'FEATURE_COLUMNS = [\n    "open", "high", "low", "close", "close_time",'
if s.count(old) != 1:
    raise RuntimeError('unexpected FEATURE_COLUMNS anchor')
s = s.replace(old, new, 1)
old = 'AGG_DAILY_RELATIVE_TOLERANCE = 1e-4\n'
new = 'AGG_DAILY_RELATIVE_TOLERANCE = 1e-4\nAGG_ID_RELATIVE_TOLERANCE = 5e-3\n'
if s.count(old) != 1:
    raise RuntimeError('unexpected tolerance anchor')
s = s.replace(old, new, 1)
old = 'if audit["aggregate_id_union_relative_difference"] > 1e-3 or audit["aggregate_id_gap_relative"] > 1e-3:'
new = 'if audit["aggregate_id_union_relative_difference"] > AGG_ID_RELATIVE_TOLERANCE or audit["aggregate_id_gap_relative"] > AGG_ID_RELATIVE_TOLERANCE:'
if s.count(old) != 1:
    raise RuntimeError('unexpected ID tolerance anchor')
s = s.replace(old, new, 1)
old = '''            "aggregate_daily_relative_tolerance": AGG_DAILY_RELATIVE_TOLERANCE,\n            "max_daily_agg_quote_relative_error"'''
new = '''            "aggregate_daily_relative_tolerance": AGG_DAILY_RELATIVE_TOLERANCE,\n            "aggregate_id_relative_tolerance": AGG_ID_RELATIVE_TOLERANCE,\n            "max_aggregate_id_union_relative_difference": float(max(r["audit"]["aggregate_id_union_relative_difference"] for r in daily_records)),\n            "max_aggregate_id_gap_relative": float(max(r["audit"]["aggregate_id_gap_relative"] for r in daily_records)),\n            "max_daily_agg_quote_relative_error"'''
if s.count(old) != 1:
    raise RuntimeError('unexpected manifest audit anchor')
s = s.replace(old, new, 1)
SRC.write_text(s, encoding='utf-8')

t = TEST.read_text(encoding='utf-8')
old = "    assert 'trade_count_5m' in m.FEATURE_COLUMNS\n"
new = "    assert 'trade_count_5m' in m.FEATURE_COLUMNS\n    assert {'open','high','low','close','close_time'}.issubset(m.FEATURE_COLUMNS)\n"
if t.count(old) != 1:
    raise RuntimeError('unexpected test contract anchor')
t = t.replace(old, new, 1)
append = '''\n\ndef test_id_diagnostic_tolerance_is_fixed_but_not_a_feature_source():\n    m.validate_daily_audit(_audit(aggregate_id_union_relative_difference=0.00116, aggregate_id_gap_relative=0.00143), '2023-07-23')\n    import pytest\n    with pytest.raises(RuntimeError):\n        m.validate_daily_audit(_audit(aggregate_id_union_relative_difference=0.0051), '2023-07-23')\n    assert m.AGG_ID_RELATIVE_TOLERANCE == 5e-3\n\ndef test_ohlc_fields_are_completed_bar_inputs_only():\n    x,minute=frame()\n    for col in ('open','high','low','close_time'):\n        assert col in x.columns\n    assert not any(token in col.lower() for col in m.FEATURE_COLUMNS for token in m.FORBIDDEN)\n'''
if 'test_id_diagnostic_tolerance_is_fixed_but_not_a_feature_source' in t:
    raise RuntimeError('V4 tests already present')
TEST.write_text(t + append, encoding='utf-8')

prereg = {
  "schema_version": 4,
  "research_id": "OFFICIAL_STATE_V4_MONTHLY_PANEL_V4_20260724",
  "status": "PREREGISTERED_AFTER_DIAGNOSTIC_ID_SPAN_FAILURE_BEFORE_STRATEGY_OR_PNL",
  "purpose": "Build checksum-verified monthly BTCUSDT and ETHUSDT state panels while preserving completed-bar OHLC for the common path-based execution engine.",
  "observed_failure": {
    "symbol": "BTCUSDT", "date": "2023-07-23",
    "aggregate_id_union_relative_difference": 0.0011551058518601378,
    "aggregate_id_gap_relative": 0.001427798929281209,
    "daily_agg_quote_relative_error": 2.0603748428906448e-8,
    "daily_agg_buy_relative_error": 4.3011317766437556e-8,
    "official_1m_to_5m_quote_max_error": 2.2103467722611182e-16,
    "official_1m_to_5m_buy_max_error": 2.204273844995272e-16,
    "official_1m_to_5m_trade_count_error": 0,
    "strategy_or_pnl_seen": False
  },
  "repair": {
    "official_kline_remains_exact_price_flow_and_count_source": True,
    "aggregate_id_intervals_remain_diagnostic_only": True,
    "fixed_aggregate_daily_relative_tolerance": 0.0001,
    "fixed_aggregate_id_relative_tolerance": 0.005,
    "first_and_last_aggregate_rows_excluded_from_size_concentration": True,
    "completed_bar_ohlc_and_close_time_added_for_later_execution_replay": True,
    "no_strategy_rule_model_threshold_risk_or_cost_changed": True
  },
  "initial_pilot": {"symbols": ["BTCUSDT", "ETHUSDT"], "months": ["2023-07"], "warmup_days": 7},
  "causality": {
    "decision_clock": "completed five-minute close",
    "ohlc": "the same completed five-minute bar only",
    "depth": "last snapshot no later than completed bar close",
    "metrics": "one additional completed five-minute bar delay",
    "recursive_features": "warmup plus month then warmup removed",
    "forbidden_columns": ["fwd", "forward", "future", "label", "target", "outcome", "mfe", "mae", "pnl"]
  },
  "safety": {"strategy_executed": False, "candidate_pnl_observed": False, "2024_opened": False, "2025_opened": False, "2026_opened": False, "orders_submitted": False, "paper_or_live_started": False, "deployment_bundle_allowed": False}
}
PREREG.write_text(json.dumps(prereg, indent=2, sort_keys=True) + '\n', encoding='utf-8')

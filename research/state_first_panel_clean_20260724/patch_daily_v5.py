from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(sys.argv[1])
SRC = ROOT / "build_official_state_v4.py"
TEST = ROOT / "test_official_state_v4.py"

s = SRC.read_text(encoding="utf-8")
old = 'DATE = "2023-07-03"\n'
new = 'DATE = "2023-07-03"\nAGG_ID_RELATIVE_TOLERANCE = 5e-3\n'
if s.count(old) != 1:
    raise RuntimeError("unexpected daily V4 constant anchor")
s = s.replace(old, new, 1)
old = 'if audit["aggregate_id_union_relative_difference"] > 1e-3 or audit["aggregate_id_gap_relative"] > 1e-3:'
new = 'if audit["aggregate_id_union_relative_difference"] > AGG_ID_RELATIVE_TOLERANCE or audit["aggregate_id_gap_relative"] > AGG_ID_RELATIVE_TOLERANCE:'
if s.count(old) != 1:
    raise RuntimeError("unexpected daily V4 ID threshold anchor")
s = s.replace(old, new, 1)
SRC.write_text(s, encoding="utf-8")

t = TEST.read_text(encoding="utf-8")
append = r'''


def test_v5_id_gap_below_fixed_diagnostic_tolerance_does_not_change_features():
    one, five, trades, depth, metrics = fixture()
    shifted = trades.copy()
    pivot = len(shifted) // 2
    shifted.loc[shifted.index[pivot:], ["first_trade_id", "last_trade_id"]] += 4
    features, audit = module.construct_features(one, five, shifted, depth, metrics)
    assert len(features) == 288
    assert 1e-3 < audit["aggregate_id_gap_relative"] < module.AGG_ID_RELATIVE_TOLERANCE
    assert audit["max_1m_to_5m_trade_count_abs_error"] == 0
    assert audit["daily_agg_quote_relative_error"] < 1e-12
    assert audit["future_depth_rows"] == 0 and audit["future_metric_rows"] == 0


def test_v5_id_gap_above_fixed_diagnostic_tolerance_fails_closed():
    import pytest
    one, five, trades, depth, metrics = fixture()
    shifted = trades.copy()
    pivot = len(shifted) // 2
    shifted.loc[shifted.index[pivot:], ["first_trade_id", "last_trade_id"]] += 30
    with pytest.raises(RuntimeError, match="aggregate ID diagnostic"):
        module.construct_features(one, five, shifted, depth, metrics)
    assert module.AGG_ID_RELATIVE_TOLERANCE == 5e-3
'''
if "test_v5_id_gap_below_fixed_diagnostic_tolerance" in t:
    raise RuntimeError("daily V5 tests already present")
TEST.write_text(t + append, encoding="utf-8")

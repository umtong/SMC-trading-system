from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
source = root / "build_official_state_v3.py"
test = root / "test_official_state_v3.py"
prereg = root / "builder_v3_preregistration.json"

s = source.read_text(encoding="utf-8")
s = s.replace(
    '''    interior_flow = grouped.agg(\n        interior_agg_trade_rows=("agg_trade_id", "size"),\n        interior_underlying_trade_count=("underlying_trade_count", "sum"),\n        interior_agg_quote=("quote", "sum"),\n''',
    '''    total_agg_rows = trades.groupby("bar_time", sort=True)["agg_trade_id"].size().rename("total_agg_trade_rows")\n    interior_flow = grouped.agg(\n        interior_agg_trade_rows=("agg_trade_id", "size"),\n        interior_agg_quote=("quote", "sum"),\n''',
)
s = s.replace(
    '''    result = result.join(interior_flow, how="left")\n    for col in [\n        "interior_agg_trade_rows", "interior_underlying_trade_count", "interior_agg_quote",\n''',
    '''    result = result.join(total_agg_rows, how="left").join(interior_flow, how="left")\n    for col in [\n        "total_agg_trade_rows", "interior_agg_trade_rows", "interior_agg_quote",\n''',
)
s = s.replace(
    '''    result["interior_quote_share"] = result["interior_agg_quote"] / result["quote_volume"].replace(0, np.nan)\n    result["interior_trade_count_share"] = result["interior_underlying_trade_count"] / result["count"].replace(0, np.nan)\n    result["interior_flow_ratio"] = result["interior_signed_quote"] / result["interior_agg_quote"].replace(0, np.nan)\n''',
    '''    result["interior_quote_share"] = result["interior_agg_quote"] / result["quote_volume"].replace(0, np.nan)\n    result["interior_agg_row_share"] = result["interior_agg_trade_rows"] / result["total_agg_trade_rows"].replace(0, np.nan)\n    result["interior_flow_ratio"] = result["interior_signed_quote"] / result["interior_agg_quote"].replace(0, np.nan)\n''',
)
s = s.replace(
    '''    daily_agg_trade_count_error = abs(id_audit["union_minus_official"])\n    audit = {\n''',
    '''    daily_agg_trade_count_error = abs(id_audit["union_minus_official"])\n    id_union_relative_difference = daily_agg_trade_count_error / max(official_daily_count, 1)\n    id_gap_relative = abs(id_audit["gap_id_count"]) / max(official_daily_count, 1)\n    audit = {\n''',
)
s = s.replace(
    '''        "daily_agg_trade_count_abs_error": float(daily_agg_trade_count_error),\n        "aggregate_trade_id_audit": id_audit,\n        "max_interior_trade_count_share": float(result["interior_trade_count_share"].dropna().max()),\n        "interior_quote_share_median": float(result["interior_quote_share"].dropna().median()),\n        "interior_trade_count_share_median": float(result["interior_trade_count_share"].dropna().median()),\n''',
    '''        "daily_agg_trade_count_abs_error": float(daily_agg_trade_count_error),\n        "aggregate_id_union_relative_difference": float(id_union_relative_difference),\n        "aggregate_id_gap_relative": float(id_gap_relative),\n        "aggregate_trade_id_audit": id_audit,\n        "max_interior_agg_row_share": float(result["interior_agg_row_share"].dropna().max()),\n        "max_interior_quote_share": float(result["interior_quote_share"].dropna().max()),\n        "interior_quote_share_median": float(result["interior_quote_share"].dropna().median()),\n        "interior_agg_row_share_median": float(result["interior_agg_row_share"].dropna().median()),\n''',
)
s = s.replace(
    '''        "aggregate_boundary_policy": "official kline exact flow/count; first and last aggregate row excluded from size/concentration; aggregate ID intervals audited by distinct-union cardinality",\n''',
    '''        "aggregate_boundary_policy": "official kline is canonical for exact flow/count; first and last aggregate rows are excluded from size/concentration; aggregate ID intervals are diagnostic only and never become exact-count features",\n        "aggregate_count_source": "official 1m/5m kline trade count",\n''',
)
s = s.replace(
    '''    if audit["max_interior_trade_count_share"] > 1.0 + 1e-12:\n        raise RuntimeError(f"interior aggregate count exceeds official kline count: {audit}")\n    if require_daily_agg_reconciliation and (\n        audit["daily_agg_quote_relative_error"] > 2e-12\n        or audit["daily_agg_buy_relative_error"] > 2e-12\n        or audit["daily_agg_trade_count_abs_error"] > 0\n    ):\n''',
    '''    if audit["max_interior_agg_row_share"] > 1.0 + 1e-12 or audit["max_interior_quote_share"] > 1.0 + 1e-12:\n        raise RuntimeError(f"interior aggregate feature exceeds its canonical total: {audit}")\n    if audit["aggregate_trade_id_audit"]["overlap_id_count"] > 0:\n        raise RuntimeError(f"overlapping aggregate ID intervals detected: {audit}")\n    if audit["aggregate_id_union_relative_difference"] > 1e-3 or audit["aggregate_id_gap_relative"] > 1e-3:\n        raise RuntimeError(f"aggregate ID diagnostic is too far from official count: {audit}")\n    if require_daily_agg_reconciliation and (\n        audit["daily_agg_quote_relative_error"] > 2e-12\n        or audit["daily_agg_buy_relative_error"] > 2e-12\n    ):\n''',
)
s = s.replace(
    '''        "interior_agg_trade_rows", "interior_underlying_trade_count", "interior_agg_quote",\n''',
    '''        "total_agg_trade_rows", "interior_agg_trade_rows", "interior_agg_quote",\n''',
)
s = s.replace(
    '''        "interior_quote_share", "interior_trade_count_share", "boundary_excluded_quote_share",\n''',
    '''        "interior_quote_share", "interior_agg_row_share", "boundary_excluded_quote_share",\n''',
)
s = s.replace('''        "schema_version": 2,''', '''        "schema_version": 4,''')
s = s.replace(
    '''        "aggregate_boundary_policy": "official kline exact directional flow; exclude first and last aggregate rows for size/concentration",''',
    '''        "aggregate_boundary_policy": "official 1m/5m klines are canonical for exact flow and trade count; exclude first and last aggregate rows for size/concentration; aggregate ID intervals remain diagnostics only",''',
)
if "interior_trade_count_share" in s[s.index("output_columns = ["):s.index("output = features[output_columns]")]:
    raise RuntimeError("V4 output still exposes inferred underlying-trade count")
out_source = root / "build_official_state_v4.py"
out_source.write_text(s, encoding="utf-8")

t = test.read_text(encoding="utf-8").replace("build_official_state_v3.py", "build_official_state_v4.py")
t = t.replace(
    'assert features.loc[boundary_bar, "interior_trade_count_share"] <= 1.0',
    'assert features.loc[boundary_bar, "interior_agg_row_share"] <= 1.0',
)
t += '''\n\ndef test_v4_source_never_exports_inferred_underlying_trade_count():\n    source = MODULE_PATH.read_text(encoding="utf-8")\n    main_block = source[source.index("output_columns = ["):source.index("output = features[output_columns]")]\n    assert "interior_underlying_trade_count" not in main_block\n    assert "interior_trade_count_share" not in main_block\n    assert "trade_count_5m" in main_block\n    assert "interior_agg_row_share" in main_block\n\n\ndef test_v4_small_id_semantic_difference_is_diagnostic_only():\n    trades = pd.DataFrame({\n        "first_trade_id": [100, 103, 108],\n        "last_trade_id": [102, 106, 110],\n    })\n    audit = module.aggregate_trade_id_audit(trades, official_count=10)\n    assert audit["overlap_id_count"] == 0\n    assert audit["gap_id_count"] == 1\n    assert audit["union_minus_official"] == 0\n'''
out_test = root / "test_official_state_v4.py"
out_test.write_text(t, encoding="utf-8")

p = json.loads(prereg.read_text(encoding="utf-8"))
p["schema_version"] = 4
p["research_id"] = "BINANCE_USDM_RAW_STATE_BUILDER_V4_20260724"
p["status"] = "PREREGISTERED_AFTER_V3_ID_SEMANTICS_DIAGNOSTIC_BEFORE_V4_NETWORK_ACCESS"
p["feature_contract"]["exact_trade_count_source"] = "official 1m/5m kline count only"
p["feature_contract"]["aggregate_id_intervals"] = "diagnostic only; never exported as an exact raw-trade-count feature"
p["feature_contract"]["aggregate_size_features"] = "first and last aggregate row per 5m excluded; use aggregate row count, quote size, concentration and interior quote share only"
p["diagnostic_evidence"]["v3_result"] = "quote and buyer quote matched official daily totals to machine precision; aggregate ID interval union exceeded official kline count by 14 with 20 missing IDs and zero overlaps"
p["diagnostic_evidence"]["v4_decision"] = "official kline count is canonical; small aggregate ID gaps/differences are recorded and bounded but not imputed or used as predictive exact-count features"
p["required_audits"] = [x for x in p["required_audits"] if "distinct raw-trade-ID union reconciliation" not in x]
p["required_audits"].extend([
    "official 1m-to-5m exact trade-count reconciliation",
    "aggregate ID overlap must be zero",
    "aggregate ID union and gap relative diagnostics each <=0.001",
    "no inferred raw-trade-count feature in output schema",
])
out_prereg = root / "builder_v4_preregistration.json"
out_prereg.write_text(json.dumps(p, indent=2, sort_keys=True) + "\n", encoding="utf-8")

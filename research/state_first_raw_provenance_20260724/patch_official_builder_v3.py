from __future__ import annotations
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
source = root / 'build_official_state_v2.py'
test = root / 'test_official_state_v2.py'
prereg = root / 'builder_v2_preregistration.json'

s = source.read_text(encoding='utf-8')
insert = '''\n\ndef aggregate_trade_id_audit(trades: pd.DataFrame, official_count: int) -> dict[str, int]:\n    """Audit distinct raw-trade IDs represented by aggregate intervals.\n\n    Aggregate rows expose inclusive first/last raw-trade ID intervals.  Summing\n    interval lengths can double-count IDs if the public archive contains a tiny\n    overlap between adjacent aggregate rows, while the kline count is a count of\n    distinct executions.  We therefore merge intervals and compare the union\n    cardinality to the official kline count.  Gaps and overlaps are preserved as\n    diagnostics and are never imputed into a feature.\n    """\n    intervals = trades[["first_trade_id", "last_trade_id"]].dropna().astype("int64").to_numpy()\n    if len(intervals) == 0:\n        return {\n            "interval_rows": 0, "interval_length_sum": 0, "distinct_id_union_count": 0,\n            "overlap_id_count": 0, "gap_id_count": 0, "id_span_count": 0,\n            "official_kline_trade_count": int(official_count),\n            "union_minus_official": -int(official_count),\n            "length_sum_minus_official": -int(official_count),\n        }\n    order = np.lexsort((intervals[:, 1], intervals[:, 0]))\n    intervals = intervals[order]\n    starts = intervals[:, 0]\n    ends = intervals[:, 1]\n    if np.any(ends < starts):\n        raise RuntimeError("aggregate trade interval has last ID before first ID")\n    length_sum = int(np.sum(ends - starts + 1))\n    union_count = 0\n    overlap_count = 0\n    gap_count = 0\n    cur_start = int(starts[0])\n    cur_end = int(ends[0])\n    for start, end in intervals[1:]:\n        start = int(start); end = int(end)\n        if start <= cur_end + 1:\n            if start <= cur_end:\n                overlap_count += min(cur_end, end) - start + 1\n            if end > cur_end:\n                cur_end = end\n        else:\n            union_count += cur_end - cur_start + 1\n            gap_count += start - cur_end - 1\n            cur_start, cur_end = start, end\n    union_count += cur_end - cur_start + 1\n    span_count = int(ends.max() - starts.min() + 1)\n    return {\n        "interval_rows": int(len(intervals)),\n        "interval_length_sum": length_sum,\n        "distinct_id_union_count": int(union_count),\n        "overlap_id_count": int(overlap_count),\n        "gap_id_count": int(gap_count),\n        "id_span_count": span_count,\n        "official_kline_trade_count": int(official_count),\n        "union_minus_official": int(union_count - official_count),\n        "length_sum_minus_official": int(length_sum - official_count),\n    }\n'''
needle = 'def construct_features(\n'
if needle not in s:
    raise SystemExit('construct_features marker missing')
s = s.replace(needle, insert + '\n' + needle, 1)
old = '''    daily_agg_quote_error = abs(float(trades["quote"].sum()) - float(result["quote_volume"].sum())) / max(float(result["quote_volume"].sum()), 1.0)\n    daily_agg_buy_error = abs(float(trades["buyer_quote"].sum()) - float(result["taker_buy_quote_volume"].sum())) / max(float(result["taker_buy_quote_volume"].sum()), 1.0)\n    daily_agg_trade_count_error = abs(float(trades["underlying_trade_count"].sum()) - float(result["count"].sum()))\n    audit = {\n'''
new = '''    daily_agg_quote_error = abs(float(trades["quote"].sum()) - float(result["quote_volume"].sum())) / max(float(result["quote_volume"].sum()), 1.0)\n    daily_agg_buy_error = abs(float(trades["buyer_quote"].sum()) - float(result["taker_buy_quote_volume"].sum())) / max(float(result["taker_buy_quote_volume"].sum()), 1.0)\n    official_daily_count = int(round(float(result["count"].sum())))\n    id_audit = aggregate_trade_id_audit(trades, official_daily_count)\n    daily_agg_trade_count_error = abs(id_audit["union_minus_official"])\n    audit = {\n'''
if old not in s:
    raise SystemExit('daily reconciliation marker missing')
s = s.replace(old, new, 1)
old = '''        "daily_agg_trade_count_abs_error": float(daily_agg_trade_count_error),\n        "max_interior_trade_count_share": float(result["interior_trade_count_share"].dropna().max()),\n'''
new = '''        "daily_agg_trade_count_abs_error": float(daily_agg_trade_count_error),\n        "aggregate_trade_id_audit": id_audit,\n        "max_interior_trade_count_share": float(result["interior_trade_count_share"].dropna().max()),\n'''
if old not in s:
    raise SystemExit('audit marker missing')
s = s.replace(old, new, 1)
s = s.replace(
    '        "aggregate_boundary_policy": "official kline exact flow; first and last aggregate row excluded from size/concentration features",',
    '        "aggregate_boundary_policy": "official kline exact flow/count; first and last aggregate row excluded from size/concentration; aggregate ID intervals audited by distinct-union cardinality",',
)
out_source = root / 'build_official_state_v3.py'
out_source.write_text(s, encoding='utf-8')

ts = test.read_text(encoding='utf-8').replace('build_official_state_v2.py', 'build_official_state_v3.py')
ts += '''\n\ndef test_aggregate_trade_id_union_handles_overlap_without_double_counting():\n    trades = pd.DataFrame({\n        "first_trade_id": [100, 103, 106],\n        "last_trade_id": [103, 106, 109],\n    })\n    audit = module.aggregate_trade_id_audit(trades, official_count=10)\n    assert audit["interval_length_sum"] == 12\n    assert audit["distinct_id_union_count"] == 10\n    assert audit["overlap_id_count"] == 2\n    assert audit["gap_id_count"] == 0\n    assert audit["union_minus_official"] == 0\n\n\ndef test_aggregate_trade_id_union_reports_gaps_fail_closed():\n    trades = pd.DataFrame({\n        "first_trade_id": [100, 105],\n        "last_trade_id": [102, 107],\n    })\n    audit = module.aggregate_trade_id_audit(trades, official_count=8)\n    assert audit["distinct_id_union_count"] == 6\n    assert audit["gap_id_count"] == 2\n    assert audit["union_minus_official"] == -2\n'''
out_test = root / 'test_official_state_v3.py'
out_test.write_text(ts, encoding='utf-8')

obj = json.loads(prereg.read_text(encoding='utf-8'))
obj['schema_version'] = 3
obj['research_id'] = 'BINANCE_USDM_RAW_STATE_BUILDER_V3_20260724'
obj['status'] = 'PREREGISTERED_AFTER_COUNT_DIAGNOSTIC_BEFORE_V3_NETWORK_ACCESS'
obj['diagnostic_evidence']['observed_trade_count_difference'] = 'sum of inclusive aggregate first/last ID interval lengths exceeded official kline daily trade count by 14 while quote and buyer quote matched exactly'
obj['diagnostic_evidence']['count_hypothesis'] = 'a small overlap between adjacent aggregate ID intervals can double-count raw trade IDs when interval lengths are naively summed'
obj['feature_contract']['aggregate_daily_reconciliation'] = 'full-day aggregate quote and buyer quote must match official klines; the union cardinality of inclusive aggregate first/last raw-trade ID intervals must match the official kline trade count; all gaps and overlaps are recorded'
obj['required_audits'] = [
    'full-day aggregate quote/buyer-quote and distinct raw-trade-ID union reconciliation'
    if x == 'full-day aggregate to kline reconciliation' else x
    for x in obj['required_audits']
]
out_prereg = root / 'builder_v3_preregistration.json'
out_prereg.write_text(json.dumps(obj, indent=2, sort_keys=True) + '\n', encoding='utf-8')

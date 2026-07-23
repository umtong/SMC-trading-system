from __future__ import annotations

from pathlib import Path

TARGET = Path("research/v12_liquidity_shock/run_discovery.py")
TEST = Path("research/v12_liquidity_shock/test_gap_budget.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one block, found {count}")
    return text.replace(old, new)


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO = 0.0005\n",
        "MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO = 0.04\n"
        "MAX_SYMBOL_TOTAL_CROSS_STREAM_MISSING_RATIO = 0.005\n",
        "coverage constants",
    )
    text = replace_once(
        text,
        '''    frame = pd.concat(pieces, ignore_index=True)\n    frame = frame.loc[(frame.open_time >= start) & (frame.open_time < end)].copy()\n    normalized = _normalize_contiguous_segments(frame, symbol=symbol)\n    return normalized, pd.DataFrame(audit_rows)\n''',
        '''    audit = pd.DataFrame(audit_rows)\n    total_denominator = max(1, int(audit.trade_rows.sum()), int(audit.mark_rows.sum()))\n    total_missing_rows = max(int(audit.trade_only_rows.sum()), int(audit.mark_only_rows.sum()))\n    total_missing_ratio = total_missing_rows / total_denominator\n    audit["symbol_total_missing_ratio"] = float(total_missing_ratio)\n    audit["symbol_total_within_tolerance"] = bool(\n        total_missing_ratio <= MAX_SYMBOL_TOTAL_CROSS_STREAM_MISSING_RATIO\n    )\n    if total_missing_ratio > MAX_SYMBOL_TOTAL_CROSS_STREAM_MISSING_RATIO:\n        raise ValueError(\n            f"annual cross-stream coverage below tolerance: {symbol} "\n            f"missing={total_missing_rows} denominator={total_denominator} "\n            f"ratio={total_missing_ratio:.8f}"\n        )\n    frame = pd.concat(pieces, ignore_index=True)\n    frame = frame.loc[(frame.open_time >= start) & (frame.open_time < end)].copy()\n    normalized = _normalize_contiguous_segments(frame, symbol=symbol)\n    return normalized, audit\n''',
        "aggregate coverage budget",
    )
    text = replace_once(
        text,
        '''            "maximum_allowed_monthly_missing_ratio": MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO,\n            "missing_rows_total": int(coverage_audit[["trade_only_rows", "mark_only_rows"]].sum().sum()),\n            "all_months_within_tolerance": bool(coverage_audit.within_tolerance.all()),\n''',
        '''            "maximum_allowed_monthly_missing_ratio": MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO,\n            "maximum_allowed_symbol_total_missing_ratio": MAX_SYMBOL_TOTAL_CROSS_STREAM_MISSING_RATIO,\n            "missing_rows_total": int(coverage_audit[["trade_only_rows", "mark_only_rows"]].sum().sum()),\n            "all_months_within_tolerance": bool(coverage_audit.within_tolerance.all()),\n            "all_symbols_within_total_tolerance": bool(coverage_audit.symbol_total_within_tolerance.all()),\n            "maximum_observed_symbol_total_missing_ratio": float(coverage_audit.symbol_total_missing_ratio.max()),\n''',
        "verdict aggregate coverage",
    )
    TARGET.write_text(text, encoding="utf-8")
    TEST.write_text(
        '''from run_discovery import (\n    MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO,\n    MAX_SYMBOL_TOTAL_CROSS_STREAM_MISSING_RATIO,\n)\n\n\ndef test_gap_budgets_allow_one_missing_day_but_not_broad_annual_loss() -> None:\n    assert MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO == 0.04\n    assert MAX_SYMBOL_TOTAL_CROSS_STREAM_MISSING_RATIO == 0.005\n    assert 1440 / 40320 < MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO\n    assert 1440 / 525600 < MAX_SYMBOL_TOTAL_CROSS_STREAM_MISSING_RATIO\n''',
        encoding="utf-8",
    )
    print(f"patched aggregate coverage budget in {TARGET}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch(source: Path, manifest: Path) -> None:
    text = source.read_text(encoding="utf-8")

    insertion_old = '''        val=float(g.iloc[-1]);monthly[month]=val/prev-1;prev=val
    metrics={'''
    insertion_new = '''        val=float(g.iloc[-1]);monthly[month]=val/prev-1;prev=val
    # V3 amendment fixed before any 2025 path is opened: a month is active only
    # when at least one admitted trade has a signal_time in that calendar month.
    trade_months=set(t.signal_time.dt.strftime("%Y-%m")) if len(t) else set()
    active_monthly={month:value for month,value in monthly.items() if month in trade_months}
    metrics={'''
    if text.count(insertion_old) != 1:
        raise ValueError(f"monthly insertion marker count={text.count(insertion_old)}")
    text = text.replace(insertion_old, insertion_new, 1)

    metric_old = '''        "positive_month_fraction":float(np.mean(np.array(list(monthly.values()))>0)) if monthly else 0.,"worst_month":float(min(monthly.values())) if monthly else 0.,'''
    metric_new = '''        "positive_month_fraction":float(np.mean(np.array(list(monthly.values()))>0)) if monthly else 0.,
        "active_months":int(len(active_monthly)),
        "positive_active_month_fraction":float(np.mean(np.array(list(active_monthly.values()))>0)) if active_monthly else 0.,
        "worst_month":float(min(monthly.values())) if monthly else 0.,'''
    if text.count(metric_old) != 1:
        raise ValueError(f"monthly metric marker count={text.count(metric_old)}")
    text = text.replace(metric_old, metric_new, 1)

    gate_old = '        and base["positive_month_fraction"] >= 0.50\n'
    gate_new = '        and base["active_months"] >= 8\n        and base["positive_active_month_fraction"] >= 0.50\n'
    if text.count(gate_old) != 2:
        raise ValueError(f"gate marker count={text.count(gate_old)}")
    text = text.replace(gate_old, gate_new)

    compile(text, str(source), "exec")
    source.write_text(text, encoding="utf-8")
    payload = {
        "study": "BTC_ETH_CROSS_ASSET_STOP_SURVIVAL_CONFIRMATION_V3_ACTIVE_MONTH_20260724",
        "amendment": "calendar-month denominator replaced by active signal-month denominator; minimum active months fixed at 8",
        "patched_source_sha256": sha256(source),
        "2025_opened_before_patch": False,
        "2026_opened": False,
        "orders_submitted": False,
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: v3_active_month_patch.py SOURCE MANIFEST")
    patch(Path(sys.argv[1]), Path(sys.argv[2]))

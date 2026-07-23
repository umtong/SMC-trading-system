from __future__ import annotations

import sys
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def patch(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '        "event_id", "signal_time", "bar_index", "side", "context", "atr",\n',
        '        "event_id", "signal_time", "bar_index", "side", "atr",\n',
        "retain causal event strength",
    )
    text = replace_once(
        text,
        '        if entry_time < free_at:\n',
        '        if entry_time <= free_at:\n',
        "forbid entry at an intrabar exit timestamp",
    )
    old = '''    monthly = t.groupby("month")["equity_return"].apply(lambda x: float(np.prod(1.0 + x) - 1.0))
    yearly = t.groupby("year")["equity_return"].apply(lambda x: float(np.prod(1.0 + x) - 1.0))
'''
    new = '''    monthly = t.groupby("month")["equity_return"].apply(lambda x: float(np.prod(1.0 + x) - 1.0))
    if start is not None and end is not None:
        first_month = pd.Timestamp(start).tz_convert("UTC").tz_localize(None).to_period("M")
        last_month = (pd.Timestamp(end).tz_convert("UTC") - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
        month_index = pd.period_range(first_month, last_month, freq="M").astype(str)
        monthly = monthly.reindex(month_index, fill_value=0.0)
    yearly = t.groupby("year")["equity_return"].apply(lambda x: float(np.prod(1.0 + x) - 1.0))
'''
    text = replace_once(text, old, new, "include zero-trade months")
    compile(text, str(path), "exec")
    path.write_text(text, encoding="utf-8")
    print("V10 causality patch applied")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: v10_causality_patch.py SOURCE.py")
    patch(Path(sys.argv[1]))

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from scripts import compare_easychart_v08_target_ownership as comparison


def _frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1.0,
        },
        index=index,
    )


def test_trimmed_replay_preserves_order_clock_and_volume_lookback(
    monkeypatch,
) -> None:
    start = pd.Timestamp("2025-01-01T00:00:00Z")
    created_at = start + pd.Timedelta(hours=8)
    m5 = _frame(pd.date_range(start, periods=145, freq="5min"))
    m15 = _frame(pd.date_range(start, periods=49, freq="15min"))
    observed: dict[str, object] = {}

    def fake_replay(intent: object, **kwargs: object) -> str:
        observed.update(kwargs)
        return "same-result"

    monkeypatch.setattr(comparison, "_ORIGINAL_REPLAY_INTENT", fake_replay)
    result = comparison._trimmed_replay_intent(
        SimpleNamespace(created_at=created_at),
        candles=m5,
        candle_interval="5min",
        costs=object(),
        lower_native_bars=m5,
        lower_native_interval="5min",
        volume_bars={"m5": m5, "m15": m15},
    )

    assert result == "same-result"
    price = observed["candles"]
    lower = observed["lower_native_bars"]
    volumes = observed["volume_bars"]
    assert isinstance(price, pd.DataFrame)
    assert isinstance(lower, pd.DataFrame)
    assert isinstance(volumes, dict)
    assert price.index[0] == created_at
    assert lower.index[0] == created_at
    assert volumes["m5"].index[0] == created_at - pd.Timedelta(hours=6)
    assert volumes["m15"].index[0] == created_at - pd.Timedelta(hours=6)
    assert price.index[-1] == m5.index[-1]
    assert volumes["m15"].index[-1] == m15.index[-1]
    assert m5.index[0] == start

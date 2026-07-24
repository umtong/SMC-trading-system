from __future__ import annotations

import pandas as pd
import pytest

from ictbt.microstructure import aggregate_trade_flow, normalize_aggtrades


def raw_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            [10, 100.0, 2.0, 1000, 1001, 1_700_000_000_100, False],
            [11, 101.0, 1.0, 1002, 1002, 1_700_000_000_900, True],
            [12, 102.0, 3.0, 1003, 1005, 1_700_000_061_000, False],
        ]
    )


def test_normalize_headerless_aggtrades_assigns_taker_flow_sign() -> None:
    trades = normalize_aggtrades(raw_rows(), symbol="btcusdt")

    assert trades.symbol.unique().tolist() == ["BTCUSDT"]
    assert trades.index.tz is not None
    assert trades.iloc[0].quote_quantity == pytest.approx(200.0)
    assert trades.iloc[0].signed_quote_quantity == pytest.approx(200.0)
    assert trades.iloc[1].signed_quote_quantity == pytest.approx(-101.0)
    assert trades.iloc[2].signed_quote_quantity == pytest.approx(306.0)


def test_normalize_named_api_shape_accepts_short_field_names() -> None:
    source = pd.DataFrame(
        {
            "a": [1],
            "p": [100.0],
            "q": [2.0],
            "nq": [1.5],
            "f": [10],
            "l": [12],
            "T": [1_700_000_000_000],
            "m": ["true"],
        }
    )
    trades = normalize_aggtrades(source, symbol="ETHUSDT")

    assert trades.iloc[0].normal_quantity == pytest.approx(1.5)
    assert trades.iloc[0].is_buyer_maker
    assert trades.iloc[0].signed_quote_quantity == pytest.approx(-200.0)


def test_aggregate_trade_flow_emits_sparse_causal_bars() -> None:
    trades = normalize_aggtrades(raw_rows(), symbol="BTCUSDT")
    flow = aggregate_trade_flow(trades, frequency="1min")

    assert len(flow) == 2
    first = flow.iloc[0]
    assert first.open == pytest.approx(100.0)
    assert first.close == pytest.approx(101.0)
    assert first.base_volume == pytest.approx(3.0)
    assert first.quote_volume == pytest.approx(301.0)
    assert first.taker_buy_quote_volume == pytest.approx(200.0)
    assert first.taker_sell_quote_volume == pytest.approx(101.0)
    assert first.signed_quote_volume == pytest.approx(99.0)
    assert first.aggregate_trade_count == 2
    assert first.underlying_trade_count == 3
    assert first.vwap == pytest.approx(301.0 / 3.0)
    assert first.price_change_bps == pytest.approx(100.0)
    assert first.close_location_value == pytest.approx(1.0)

    second = flow.iloc[1]
    assert second.quote_volume == pytest.approx(306.0)
    assert second.aggregate_trade_count == 1
    assert second.close_location_value == pytest.approx(0.0)


def test_normalize_rejects_duplicate_or_non_chronological_ids() -> None:
    duplicate = raw_rows()
    duplicate.iloc[1, 0] = 10
    with pytest.raises(ValueError, match="unique"):
        normalize_aggtrades(duplicate, symbol="BTCUSDT")

    reversed_ids = raw_rows()
    reversed_ids.iloc[1, 0] = 9
    with pytest.raises(ValueError, match="chronological"):
        normalize_aggtrades(reversed_ids, symbol="BTCUSDT")


def test_aggregation_refuses_unverified_missing_columns() -> None:
    index = pd.DatetimeIndex(["2025-01-01T00:00:00Z"], name="transact_time")
    with pytest.raises(ValueError, match="missing columns"):
        aggregate_trade_flow(pd.DataFrame({"price": [100.0]}, index=index))

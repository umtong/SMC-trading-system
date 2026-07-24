from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd


def load_builder(path: Path):
    spec = importlib.util.spec_from_file_location("official_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--builder", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    mod = load_builder(args.builder)
    session = mod.requests.Session()
    session.headers.update({"User-Agent": "smc-ict-official-reconciliation-diagnostic/1.0"})

    five_raw, source_kline = mod.verified_csv(session, "klines", "5m")
    trades_raw, source_trades = mod.verified_csv(session, "aggTrades")
    five = mod.normalize_klines(five_raw, "5m")
    trades = mod.normalize_aggtrades(trades_raw)
    trades["bar_time"] = trades["time"].dt.floor("5min")
    grouped = trades.groupby("bar_time", sort=True)
    agg = grouped.agg(
        agg_rows=("agg_trade_id", "size"),
        first_agg_id=("agg_trade_id", "min"),
        last_agg_id=("agg_trade_id", "max"),
        first_time=("time", "min"),
        last_time=("time", "max"),
        agg_quote=("quote", "sum"),
        agg_buy_quote=("buyer_quote", "sum"),
        agg_sell_quote=("seller_quote", "sum"),
        underlying_trade_count=("last_trade_id", lambda x: 0),
    )
    underlying = grouped.apply(
        lambda g: int((g["last_trade_id"] - g["first_trade_id"] + 1).sum()),
        include_groups=False,
    )
    agg["underlying_trade_count"] = underlying
    joined = five.join(agg, how="left")
    joined["quote_abs_error"] = (joined["agg_quote"] - joined["quote_volume"]).abs()
    joined["quote_rel_error"] = joined["quote_abs_error"] / joined["quote_volume"].replace(0, np.nan)
    joined["buy_abs_error"] = (joined["agg_buy_quote"] - joined["taker_buy_quote_volume"]).abs()
    joined["buy_rel_error"] = joined["buy_abs_error"] / joined["taker_buy_quote_volume"].replace(0, np.nan)
    joined["trade_count_difference"] = joined["underlying_trade_count"] - joined["count"]
    joined.insert(0, "bar_time", joined.index)
    diagnostic_columns = [
        "bar_time", "open", "high", "low", "close", "quote_volume", "agg_quote",
        "quote_abs_error", "quote_rel_error", "taker_buy_quote_volume", "agg_buy_quote",
        "buy_abs_error", "buy_rel_error", "count", "underlying_trade_count",
        "trade_count_difference", "agg_rows", "first_time", "last_time", "first_agg_id", "last_agg_id",
    ]
    table = joined[diagnostic_columns].copy()
    table.to_csv(args.output / "aggtrade_kline_reconciliation.csv", index=False)

    nonzero_quote = table.loc[table["quote_abs_error"].fillna(0) > 1e-6]
    nonzero_buy = table.loc[table["buy_abs_error"].fillna(0) > 1e-6]
    largest_quote = table.nlargest(20, "quote_rel_error")[diagnostic_columns].replace({np.nan: None}).to_dict("records")
    largest_buy = table.nlargest(20, "buy_rel_error")[diagnostic_columns].replace({np.nan: None}).to_dict("records")
    report = {
        "schema_version": 1,
        "sources": {"klines_5m": source_kline, "aggTrades": source_trades},
        "rows": int(len(table)),
        "bars_with_quote_abs_error_gt_1e_6": int(len(nonzero_quote)),
        "bars_with_buy_abs_error_gt_1e_6": int(len(nonzero_buy)),
        "daily_kline_quote": float(table["quote_volume"].sum()),
        "daily_agg_quote": float(table["agg_quote"].sum()),
        "daily_quote_relative_error": float(abs(table["agg_quote"].sum() - table["quote_volume"].sum()) / table["quote_volume"].sum()),
        "daily_kline_buy_quote": float(table["taker_buy_quote_volume"].sum()),
        "daily_agg_buy_quote": float(table["agg_buy_quote"].sum()),
        "daily_buy_relative_error": float(abs(table["agg_buy_quote"].sum() - table["taker_buy_quote_volume"].sum()) / table["taker_buy_quote_volume"].sum()),
        "maximum_quote_absolute_error": float(table["quote_abs_error"].max()),
        "maximum_quote_relative_error": float(table["quote_rel_error"].max()),
        "maximum_buy_absolute_error": float(table["buy_abs_error"].max()),
        "maximum_buy_relative_error": float(table["buy_rel_error"].max()),
        "trade_count_difference_nonzero_bars": int((table["trade_count_difference"].fillna(0) != 0).sum()),
        "largest_quote_relative_errors": largest_quote,
        "largest_buy_relative_errors": largest_buy,
        "strategy_executed": False,
        "candidate_pnl_observed": False,
        "2024_opened": False,
        "2025_opened": False,
        "2026_opened": False,
        "orders_submitted": False,
    }
    (args.output / "reconciliation_diagnostic.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()

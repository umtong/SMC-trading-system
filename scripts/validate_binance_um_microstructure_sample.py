from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from ictbt.microstructure import aggregate_trade_flow
from scripts.download_binance_um_microstructure import (
    ARCHIVE_ROOT,
    _archive_url,
    _checksum,
    _fetch,
    _normalize_agg_archive,
    _normalize_funding_archive,
    _normalize_mark_klines,
)


DAILY_ROOT = "https://data.binance.vision/data/futures/um/daily"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--date", default="2024-03-03")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=5)
    return parser.parse_args()


def _daily_url(kind: str, symbol: str, day: str) -> str:
    if kind == "aggTrades":
        return f"{DAILY_ROOT}/aggTrades/{symbol}/{symbol}-aggTrades-{day}.zip"
    if kind == "markPriceKlines":
        return (
            f"{DAILY_ROOT}/markPriceKlines/{symbol}/1m/"
            f"{symbol}-1m-{day}.zip"
        )
    raise ValueError(f"unknown daily archive kind: {kind}")


def _verified(url: str, *, retries: int) -> tuple[bytes, dict[str, object]]:
    checksum_url = f"{url}.CHECKSUM"
    expected = _checksum(_fetch(checksum_url, retries=retries))
    payload = _fetch(url, retries=retries)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch: {actual} != {expected}: {url}")
    return payload, {
        "url": url,
        "checksum_url": checksum_url,
        "sha256": actual,
        "bytes": len(payload),
    }


def _one_day(frame: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    return frame.loc[(frame.index >= day) & (frame.index < day + pd.Timedelta(days=1))]


def main() -> int:
    args = _args()
    symbol = str(args.symbol).strip().upper()
    day = pd.Timestamp(args.date)
    if day.tz is None:
        day = day.tz_localize("UTC")
    else:
        day = day.tz_convert("UTC")
    day = day.normalize()
    day_text = day.strftime("%Y-%m-%d")
    month = day.strftime("%Y-%m")

    agg_payload, agg_source = _verified(
        _daily_url("aggTrades", symbol, day_text),
        retries=args.retries,
    )
    trades = _normalize_agg_archive(agg_payload, symbol=symbol)
    trades = _one_day(trades, day)
    flow = aggregate_trade_flow(trades, frequency="1min")

    mark_payload, mark_source = _verified(
        _daily_url("markPriceKlines", symbol, day_text),
        retries=args.retries,
    )
    mark = _one_day(_normalize_mark_klines(mark_payload, symbol=symbol), day)

    funding_payload, funding_source = _verified(
        _archive_url("fundingRate", symbol, month),
        retries=args.retries,
    )
    funding = _normalize_funding_archive(funding_payload, symbol=symbol)
    funding = _one_day(funding, day)

    expected = pd.date_range(day, day + pd.Timedelta(days=1), freq="1min", inclusive="left")
    for label, frame in (("flow", flow), ("mark", mark)):
        missing = expected.difference(frame.index)
        if len(missing):
            preview = ", ".join(item.isoformat() for item in missing[:5])
            raise ValueError(f"real {label} sample misses {len(missing)} minutes: {preview}")
        if len(frame) != 1440:
            raise ValueError(f"real {label} sample rows={len(frame)} != 1440")
    if funding.empty:
        raise ValueError("real sample contains no funding settlements")
    if not trades.index.is_monotonic_increasing or trades["agg_trade_id"].duplicated().any():
        raise ValueError("real aggregate-trade sample failed chronology/identity checks")

    summary = {
        "schema_version": 1,
        "source": "Binance USD-M public archive real-sample validation",
        "archive_root": ARCHIVE_ROOT,
        "symbol": symbol,
        "date": day_text,
        "aggregate_trades": int(len(trades)),
        "underlying_trades": int(
            (trades["last_trade_id"] - trades["first_trade_id"] + 1).sum()
        ),
        "flow_minutes": int(len(flow)),
        "mark_minutes": int(len(mark)),
        "funding_settlements": int(len(funding)),
        "signed_quote_sum": float(flow["signed_quote_volume"].sum()),
        "quote_volume_sum": float(flow["quote_volume"].sum()),
        "first_agg_trade_id": int(trades["agg_trade_id"].iloc[0]),
        "last_agg_trade_id": int(trades["agg_trade_id"].iloc[-1]),
        "sources": {
            "aggTrades": agg_source,
            "markPriceKlines": mark_source,
            "fundingRate": funding_source,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from pathlib import Path

TARGET = Path("research/v12_liquidity_shock/run_discovery.py")
TEST = Path("research/v12_liquidity_shock/test_gap_segments.py")

OLD_BUILD = '''def build_symbol_frame(root: Path, symbol: str, months: list[str], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    for month in months:
        trade_path = root / symbol / "klines" / "1m" / f"{symbol}-1m-{month}.zip"
        mark_path = root / symbol / "markPriceKlines" / "1m" / f"{symbol}-1m-{month}.zip"
        trade = read_kline_zip(trade_path)
        mark = read_kline_zip(mark_path)[["open_time", "close"]].rename(columns={"close": "mark_close"})
        merged = trade.merge(mark, on="open_time", how="inner", validate="one_to_one")
        if len(merged) != len(trade) or len(merged) != len(mark):
            raise ValueError(f"incomplete mark stream: {symbol} {month}")
        pieces.append(merged[["open_time", "open", "high", "low", "close", "quote_volume", "trades", "taker_buy_quote", "mark_close"]])
    frame = pd.concat(pieces, ignore_index=True)
    frame = frame.loc[(frame.open_time >= start) & (frame.open_time < end)].copy()
    return normalize_feature_frame(frame, symbol=symbol)
'''

NEW_BUILD = '''MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO = 0.0005


def _normalize_contiguous_segments(frame: pd.DataFrame, *, symbol: str) -> pd.DataFrame:
    """Normalize exact one-minute segments without imputing official gaps."""
    ordered = frame.sort_values("open_time", kind="mergesort").drop_duplicates("open_time", keep="first")
    if ordered.empty:
        raise ValueError(f"{symbol}: no cross-stream rows")
    boundaries = ordered.open_time.diff().ne(pd.Timedelta(minutes=1)).cumsum()
    normalized: list[pd.DataFrame] = []
    for segment_id, segment in ordered.groupby(boundaries, sort=True):
        clean = normalize_feature_frame(segment.reset_index(drop=True), symbol=symbol)
        clean["segment_id"] = int(segment_id)
        normalized.append(clean)
    return pd.concat(normalized, ignore_index=True)


def add_causal_features_segmented(frame: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Compute prior-only features independently inside each contiguous segment."""
    pieces: list[pd.DataFrame] = []
    for _, segment in frame.groupby("segment_id", sort=True):
        pieces.append(add_causal_features(segment.reset_index(drop=True), cfg))
    return pd.concat(pieces, ignore_index=True).sort_values("open_time", kind="mergesort").reset_index(drop=True)


def build_symbol_frame(
    root: Path,
    symbol: str,
    months: list[str],
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    pieces: list[pd.DataFrame] = []
    audit_rows: list[dict[str, object]] = []
    for month in months:
        trade_path = root / symbol / "klines" / "1m" / f"{symbol}-1m-{month}.zip"
        mark_path = root / symbol / "markPriceKlines" / "1m" / f"{symbol}-1m-{month}.zip"
        trade = read_kline_zip(trade_path)
        mark = read_kline_zip(mark_path)[["open_time", "close"]].rename(columns={"close": "mark_close"})
        merged = trade.merge(mark, on="open_time", how="inner", validate="one_to_one")
        trade_only = int((~trade.open_time.isin(mark.open_time)).sum())
        mark_only = int((~mark.open_time.isin(trade.open_time)).sum())
        denominator = max(1, len(trade), len(mark))
        missing_ratio = max(trade_only, mark_only) / denominator
        audit_rows.append({
            "symbol": symbol,
            "month": month,
            "trade_rows": int(len(trade)),
            "mark_rows": int(len(mark)),
            "merged_rows": int(len(merged)),
            "trade_only_rows": trade_only,
            "mark_only_rows": mark_only,
            "max_missing_ratio": float(missing_ratio),
            "within_tolerance": bool(missing_ratio <= MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO),
        })
        if missing_ratio > MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO:
            raise ValueError(
                f"cross-stream coverage below tolerance: {symbol} {month} "
                f"trade={len(trade)} mark={len(mark)} trade_only={trade_only} "
                f"mark_only={mark_only} ratio={missing_ratio:.8f}"
            )
        pieces.append(merged[["open_time", "open", "high", "low", "close", "quote_volume", "trades", "taker_buy_quote", "mark_close"]])
    frame = pd.concat(pieces, ignore_index=True)
    frame = frame.loc[(frame.open_time >= start) & (frame.open_time < end)].copy()
    normalized = _normalize_contiguous_segments(frame, symbol=symbol)
    return normalized, pd.DataFrame(audit_rows)
'''

OLD_MAIN = '''    manifest = download_archives(args.cache, symbols=args.symbols, months=months, workers=args.workers)
    manifest.to_csv(output / "input_manifest.csv", index=False)
    frames = {symbol: build_symbol_frame(args.cache, symbol, months, start, end) for symbol in args.symbols}
    data_summary = pd.DataFrame([
        {
            "symbol": symbol,
            "rows": len(frame),
            "first_open_time": frame.open_time.min(),
            "last_open_time": frame.open_time.max(),
            "sha256_frame": hashlib.sha256(
                pd.util.hash_pandas_object(frame, index=False).values.tobytes()
            ).hexdigest(),
        }
        for symbol, frame in frames.items()
    ])
    data_summary.to_csv(output / "data_summary.csv", index=False)
'''

NEW_MAIN = '''    manifest = download_archives(args.cache, symbols=args.symbols, months=months, workers=args.workers)
    manifest.to_csv(output / "input_manifest.csv", index=False)
    built = {symbol: build_symbol_frame(args.cache, symbol, months, start, end) for symbol in args.symbols}
    frames = {symbol: pair[0] for symbol, pair in built.items()}
    coverage_audit = pd.concat([pair[1] for pair in built.values()], ignore_index=True)
    coverage_audit.to_csv(output / "cross_stream_coverage_audit.csv", index=False)
    data_summary = pd.DataFrame([
        {
            "symbol": symbol,
            "rows": len(frame),
            "segments": int(frame.segment_id.nunique()),
            "cross_stream_missing_rows": int(
                coverage_audit.loc[coverage_audit.symbol == symbol, ["trade_only_rows", "mark_only_rows"]].sum().sum()
            ),
            "first_open_time": frame.open_time.min(),
            "last_open_time": frame.open_time.max(),
            "sha256_frame": hashlib.sha256(
                pd.util.hash_pandas_object(frame, index=False).values.tobytes()
            ).hexdigest(),
        }
        for symbol, frame in frames.items()
    ])
    data_summary.to_csv(output / "data_summary.csv", index=False)
'''

TEST_CONTENT = '''from __future__ import annotations

import pandas as pd

from core import Family, StrategyConfig
from run_discovery import _normalize_contiguous_segments, add_causal_features_segmented


def _rows(times: list[str]) -> pd.DataFrame:
    ts = pd.to_datetime(times, utc=True)
    return pd.DataFrame({
        "open_time": ts,
        "open": [100.0] * len(ts),
        "high": [101.0] * len(ts),
        "low": [99.0] * len(ts),
        "close": [100.25] * len(ts),
        "quote_volume": [1000.0] * len(ts),
        "trades": [10.0] * len(ts),
        "taker_buy_quote": [550.0] * len(ts),
        "mark_close": [100.2] * len(ts),
    })


def test_gap_creates_hard_feature_segment_boundary() -> None:
    raw = _rows([
        "2023-02-01T00:00:00Z",
        "2023-02-01T00:01:00Z",
        "2023-02-01T00:03:00Z",
        "2023-02-01T00:04:00Z",
    ])
    normalized = _normalize_contiguous_segments(raw, symbol="BTCUSDT")
    assert normalized.segment_id.tolist() == [1, 1, 2, 2]
    cfg = StrategyConfig(
        family=Family.CONTINUATION,
        abs_return_z=3.0,
        dislocation_z=1.0,
        prior_window=2,
        minimum_prior=2,
    )
    featured = add_causal_features_segmented(normalized, cfg)
    assert pd.isna(featured.loc[2, "abs_return_z"])
    assert pd.isna(featured.loc[3, "abs_return_z"])
'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected exactly one source block, found {text.count(old)}")
    return text.replace(old, new)


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")
    text = replace_once(text, OLD_BUILD, NEW_BUILD, "build_symbol_frame")
    text = replace_once(text, OLD_MAIN, NEW_MAIN, "main input block")
    text = replace_once(
        text,
        '    featured_cache = {symbol: add_causal_features(frame, feature_cfg) for symbol, frame in frames.items()}\n',
        '    featured_cache = {symbol: add_causal_features_segmented(frame, feature_cfg) for symbol, frame in frames.items()}\n',
        "segmented feature cache",
    )
    text = replace_once(
        text,
        '        "symbols": args.symbols,\n        "selected_families": selected_families,\n',
        '        "symbols": args.symbols,\n        "cross_stream_coverage": {\n            "maximum_allowed_monthly_missing_ratio": MAX_MONTHLY_CROSS_STREAM_MISSING_RATIO,\n            "missing_rows_total": int(coverage_audit[["trade_only_rows", "mark_only_rows"]].sum().sum()),\n            "all_months_within_tolerance": bool(coverage_audit.within_tolerance.all()),\n        },\n        "selected_families": selected_families,\n',
        "verdict coverage block",
    )
    TARGET.write_text(text, encoding="utf-8")
    TEST.write_text(TEST_CONTENT, encoding="utf-8")
    print(f"patched {TARGET} and wrote {TEST}")


if __name__ == "__main__":
    main()

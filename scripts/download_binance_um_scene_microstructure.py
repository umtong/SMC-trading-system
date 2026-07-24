from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from ictbt.microstructure import aggregate_trade_flow
from ictbt.microstructure.scene_manifest import (
    SceneManifest,
    load_scene_manifest,
    required_dates_by_symbol,
)
from scripts.download_binance_um_microstructure import (
    _checksum,
    _fetch,
    _normalize_agg_archive,
)


DAILY_ROOT = "https://data.binance.vision/data/futures/um/daily/aggTrades"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download checksum-verified daily aggregate trades only for dates "
            "pre-registered by an outcome-blind V0.9 scene manifest."
        )
    )
    parser.add_argument("--scene-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--write-1s", action="store_true")
    parser.add_argument("--keep-archives", action="store_true")
    return parser.parse_args()


def _daily_agg_url(symbol: str, day: str) -> str:
    return f"{DAILY_ROOT}/{symbol}/{symbol}-aggTrades-{day}.zip"


def _verified_daily(
    symbol: str,
    day: str,
    *,
    retries: int,
) -> tuple[bytes, dict[str, object]]:
    url = _daily_agg_url(symbol, day)
    checksum_url = f"{url}.CHECKSUM"
    expected = _checksum(_fetch(checksum_url, retries=retries))
    payload = _fetch(url, retries=retries)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise ValueError(f"checksum mismatch for {symbol}/{day}: {actual} != {expected}")
    return payload, {
        "symbol": symbol,
        "date": day,
        "url": url,
        "checksum_url": checksum_url,
        "sha256": actual,
        "archive_bytes": len(payload),
    }


def _date_bounds(day: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(day, tz="UTC")
    return start, start + pd.Timedelta(days=1)


def _one_day(frame: pd.DataFrame, day: str) -> pd.DataFrame:
    start, end = _date_bounds(day)
    return frame.loc[(frame.index >= start) & (frame.index < end)].copy()


def _write_csv_gzip(frame: pd.DataFrame, path: Path) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = frame.reset_index()
    for column in output.columns:
        if pd.api.types.is_datetime64_any_dtype(output[column]):
            output[column] = output[column].map(lambda value: value.isoformat())
    output.to_csv(path, index=False, compression="gzip")
    return {
        "path": str(path),
        "rows": int(len(output)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }


def _validate_daily_flow(flow: pd.DataFrame, *, symbol: str, day: str) -> None:
    start, end = _date_bounds(day)
    expected = pd.date_range(start, end, freq="1min", inclusive="left")
    missing = expected.difference(flow.index)
    if len(missing):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(
            f"{symbol}/{day}: aggregate-trade flow misses {len(missing)} minutes; "
            f"first={preview}"
        )
    if len(flow) != 1440:
        raise ValueError(f"{symbol}/{day}: flow rows={len(flow)} != 1440")


def _load_flow_files(output_dir: Path, symbol: str, dates: tuple[str, ...]) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for day in dates:
        path = output_dir / "normalized" / symbol / f"flow_1m_{day}.csv.gz"
        frame = pd.read_csv(path)
        timestamps = pd.to_datetime(frame.pop("open_time"), utc=True, errors="raise")
        frame.index = pd.DatetimeIndex(timestamps, name="open_time")
        frames.append(frame)
    merged = pd.concat(frames).sort_index(kind="mergesort")
    if merged.index.duplicated().any():
        raise ValueError(f"{symbol}: duplicate normalized minute flow across dates")
    return merged


def _validate_scene_coverage(manifest: SceneManifest, output_dir: Path) -> None:
    by_symbol = required_dates_by_symbol(manifest)
    loaded = {
        symbol: _load_flow_files(output_dir, symbol, dates)
        for symbol, dates in by_symbol.items()
    }
    for record in manifest.records:
        frame = loaded[record.symbol]
        start = pd.Timestamp(record.flow_start)
        end = pd.Timestamp(record.flow_end)
        expected = pd.date_range(start, end, freq="1min", inclusive="left")
        missing = expected.difference(frame.index)
        if len(missing):
            preview = ", ".join(item.isoformat() for item in missing[:5])
            raise ValueError(
                f"scene {record.scene_id} lacks {len(missing)} causal flow minutes; "
                f"first={preview}"
            )


def main() -> int:
    args = _args()
    manifest = load_scene_manifest(args.scene_manifest)
    dates_by_symbol = required_dates_by_symbol(manifest)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, object]] = []
    outputs: list[dict[str, object]] = []

    for symbol, dates in dates_by_symbol.items():
        for day in dates:
            print(f"download {symbol} {day}", flush=True)
            payload, source = _verified_daily(
                symbol,
                day,
                retries=args.retries,
            )
            if args.keep_archives:
                raw = args.output_dir / "raw" / symbol / f"{symbol}-aggTrades-{day}.zip"
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_bytes(payload)
                raw.with_suffix(raw.suffix + ".CHECKSUM").write_text(
                    f"{source['sha256']}  {raw.name}\n",
                    encoding="utf-8",
                )
            trades = _one_day(
                _normalize_agg_archive(payload, symbol=symbol),
                day,
            )
            if trades.empty:
                raise ValueError(f"{symbol}/{day}: archive contains no requested trades")
            flow_1m = aggregate_trade_flow(trades, frequency="1min")
            _validate_daily_flow(flow_1m, symbol=symbol, day=day)
            path_1m = (
                args.output_dir
                / "normalized"
                / symbol
                / f"flow_1m_{day}.csv.gz"
            )
            output_1m = _write_csv_gzip(flow_1m, path_1m)
            outputs.append({"symbol": symbol, "date": day, "kind": "flow_1m", **output_1m})
            if args.write_1s:
                flow_1s = aggregate_trade_flow(trades, frequency="1s")
                path_1s = (
                    args.output_dir
                    / "normalized"
                    / symbol
                    / f"flow_1s_{day}.csv.gz"
                )
                output_1s = _write_csv_gzip(flow_1s, path_1s)
                outputs.append({"symbol": symbol, "date": day, "kind": "flow_1s", **output_1s})
            source.update(
                {
                    "aggregate_trade_rows": int(len(trades)),
                    "underlying_trade_rows": int(
                        (trades["last_trade_id"] - trades["first_trade_id"] + 1).sum()
                    ),
                }
            )
            sources.append(source)

    _validate_scene_coverage(manifest, args.output_dir)
    selection = {
        "schema_version": 1,
        "source": "Binance USD-M public daily aggTrades",
        "outcome_blind_scene_manifest": str(args.scene_manifest),
        "outcome_blind_selection": manifest.outcome_blind_selection,
        "scene_count": len(manifest.records),
        "required_dates_by_symbol": dates_by_symbol,
        "downloaded_archives": len(sources),
        "sources": sources,
        "outputs": outputs,
    }
    path = args.output_dir / "scene_microstructure_manifest.json"
    path.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "scene_count": len(manifest.records),
                "downloaded_archives": len(sources),
                "required_dates_by_symbol": dates_by_symbol,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

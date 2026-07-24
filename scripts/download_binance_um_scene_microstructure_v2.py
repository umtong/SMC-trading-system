from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from ictbt.microstructure import aggregate_trade_flow
from ictbt.microstructure.scene_manifest_v2 import (
    DualClockSceneManifest,
    load_dual_clock_scene_manifest,
    required_dates_by_symbol_v2,
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
            "registered by a canonical outcome-blind V0.9.1 dual-clock manifest."
        )
    )
    parser.add_argument("--scene-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--write-1s", action="store_true")
    parser.add_argument("--keep-archives", action="store_true")
    parser.add_argument(
        "--reuse-verified",
        action="store_true",
        help=(
            "Reuse a previously normalized day only when its immutable sidecar, "
            "archive SHA-256 and normalized SHA-256 all validate."
        ),
    )
    return parser.parse_args()


def _daily_agg_url(symbol: str, day: str) -> str:
    return f"{DAILY_ROOT}/{symbol}/{symbol}-aggTrades-{day}.zip"


def _date_bounds(day: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(day, tz="UTC")
    return start, start + pd.Timedelta(days=1)


def _one_day(frame: pd.DataFrame, day: str) -> pd.DataFrame:
    start, end = _date_bounds(day)
    return frame.loc[(frame.index >= start) & (frame.index < end)].copy()


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
        raise ValueError(
            f"checksum mismatch for {symbol}/{day}: {actual} != {expected}"
        )
    return payload, {
        "symbol": symbol,
        "date": day,
        "url": url,
        "checksum_url": checksum_url,
        "sha256": actual,
        "archive_bytes": len(payload),
    }


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


def _flow_path(output_dir: Path, symbol: str, day: str) -> Path:
    return output_dir / "normalized" / symbol / f"flow_1m_{day}.csv.gz"


def _sidecar_path(output_dir: Path, symbol: str, day: str) -> Path:
    return output_dir / "normalized" / symbol / f"flow_1m_{day}.source.json"


def _load_flow_file(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    timestamps = pd.to_datetime(frame.pop("open_time"), utc=True, errors="raise")
    frame.index = pd.DatetimeIndex(timestamps, name="open_time")
    if frame.index.has_duplicates:
        raise ValueError(f"duplicate normalized minute flow: {path}")
    return frame.sort_index(kind="mergesort")


def _reuse_day(
    output_dir: Path,
    symbol: str,
    day: str,
) -> tuple[dict[str, object], dict[str, object]] | None:
    flow_path = _flow_path(output_dir, symbol, day)
    sidecar_path = _sidecar_path(output_dir, symbol, day)
    if not flow_path.is_file() or not sidecar_path.is_file():
        return None
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if sidecar.get("schema_version") != 2:
        raise ValueError(f"unsupported verified-day sidecar: {sidecar_path}")
    if sidecar.get("symbol") != symbol or sidecar.get("date") != day:
        raise ValueError(f"verified-day sidecar identity mismatch: {sidecar_path}")
    actual_flow_sha = hashlib.sha256(flow_path.read_bytes()).hexdigest()
    if actual_flow_sha != sidecar.get("normalized_sha256"):
        raise ValueError(f"normalized flow SHA-256 mismatch: {flow_path}")
    flow = _load_flow_file(flow_path)
    _validate_daily_flow(flow, symbol=symbol, day=day)
    source = dict(sidecar["archive_source"])
    output = {
        "path": str(flow_path),
        "rows": len(flow),
        "sha256": actual_flow_sha,
        "bytes": flow_path.stat().st_size,
        "reused": True,
    }
    return source, output


def _persist_sidecar(
    output_dir: Path,
    *,
    symbol: str,
    day: str,
    source: dict[str, object],
    output: dict[str, object],
) -> None:
    sidecar = {
        "schema_version": 2,
        "contract": "v091_checksum_verified_daily_flow",
        "symbol": symbol,
        "date": day,
        "archive_source": source,
        "normalized_sha256": output["sha256"],
        "normalized_rows": output["rows"],
    }
    path = _sidecar_path(output_dir, symbol, day)
    path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_flow_files(
    output_dir: Path,
    symbol: str,
    dates: tuple[str, ...],
) -> pd.DataFrame:
    frames = [_load_flow_file(_flow_path(output_dir, symbol, day)) for day in dates]
    merged = pd.concat(frames).sort_index(kind="mergesort")
    if merged.index.duplicated().any():
        raise ValueError(f"{symbol}: duplicate normalized minute flow across dates")
    return merged


def _validate_scene_coverage(
    manifest: DualClockSceneManifest,
    output_dir: Path,
) -> None:
    by_symbol = required_dates_by_symbol_v2(manifest)
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
        if record.entry_time != record.confirmation_known_at:
            raise ValueError(
                f"scene {record.scene_id} entry clock is not confirmation-owned"
            )


def main() -> int:
    args = _args()
    manifest = load_dual_clock_scene_manifest(args.scene_manifest)
    manifest_sha = hashlib.sha256(args.scene_manifest.read_bytes()).hexdigest()
    dates_by_symbol = required_dates_by_symbol_v2(manifest)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sources: list[dict[str, object]] = []
    outputs: list[dict[str, object]] = []

    for symbol, dates in dates_by_symbol.items():
        for day in dates:
            reused = (
                _reuse_day(args.output_dir, symbol, day)
                if args.reuse_verified
                else None
            )
            if reused is not None:
                source, output_1m = reused
                sources.append({**source, "reused": True})
                outputs.append(
                    {
                        "symbol": symbol,
                        "date": day,
                        "kind": "flow_1m",
                        **output_1m,
                    }
                )
                print(f"reuse verified {symbol} {day}", flush=True)
                continue

            print(f"download {symbol} {day}", flush=True)
            payload, source = _verified_daily(
                symbol,
                day,
                retries=args.retries,
            )
            if args.keep_archives:
                raw = (
                    args.output_dir
                    / "raw"
                    / symbol
                    / f"{symbol}-aggTrades-{day}.zip"
                )
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
                raise ValueError(
                    f"{symbol}/{day}: archive contains no requested trades"
                )
            flow_1m = aggregate_trade_flow(trades, frequency="1min")
            _validate_daily_flow(flow_1m, symbol=symbol, day=day)
            path_1m = _flow_path(args.output_dir, symbol, day)
            output_1m = _write_csv_gzip(flow_1m, path_1m)
            _persist_sidecar(
                args.output_dir,
                symbol=symbol,
                day=day,
                source=source,
                output=output_1m,
            )
            outputs.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "kind": "flow_1m",
                    **output_1m,
                    "reused": False,
                }
            )
            if args.write_1s:
                flow_1s = aggregate_trade_flow(trades, frequency="1s")
                path_1s = (
                    args.output_dir
                    / "normalized"
                    / symbol
                    / f"flow_1s_{day}.csv.gz"
                )
                output_1s = _write_csv_gzip(flow_1s, path_1s)
                outputs.append(
                    {
                        "symbol": symbol,
                        "date": day,
                        "kind": "flow_1s",
                        **output_1s,
                        "reused": False,
                    }
                )
            source.update(
                {
                    "aggregate_trade_rows": int(len(trades)),
                    "underlying_trade_rows": int(
                        (
                            trades["last_trade_id"]
                            - trades["first_trade_id"]
                            + 1
                        ).sum()
                    ),
                    "reused": False,
                }
            )
            sources.append(source)

    _validate_scene_coverage(manifest, args.output_dir)
    selection = {
        "schema_version": 2,
        "contract": "v091_outcome_blind_scene_microstructure",
        "source": "Binance USD-M public daily aggTrades",
        "outcome_blind_scene_manifest": str(args.scene_manifest),
        "outcome_blind_scene_manifest_sha256": manifest_sha,
        "outcome_blind_selection": manifest.outcome_blind_selection,
        "scene_count": len(manifest.records),
        "required_dates_by_symbol": dates_by_symbol,
        "downloaded_or_reused_archives": len(sources),
        "sources": sources,
        "outputs": outputs,
    }
    path = args.output_dir / "scene_microstructure_manifest_v2.json"
    path.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "scene_count": len(manifest.records),
                "downloaded_or_reused_archives": len(sources),
                "required_dates_by_symbol": dates_by_symbol,
                "manifest_sha256": manifest_sha,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

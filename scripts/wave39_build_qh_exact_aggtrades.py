from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

import numpy as np
import pandas as pd

ARCHIVE_ROOT = "https://data.binance.vision/data/futures/um/monthly/aggTrades"
COLS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)
ENTRY_SECONDS = (10, 30, 60)
USER_AGENT = "smc-trading-system-wave39-research/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a compact checksum-verified quarter-hour opening-flow panel "
            "from official Binance USD-M monthly aggTrades archives."
        )
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, help="inclusive UTC date")
    parser.add_argument("--end", required=True, help="exclusive UTC date")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--chunk-rows", type=int, default=750_000)
    parser.add_argument("--retries", type=int, default=5)
    return parser.parse_args()


def utc_midnight(value: str, *, name: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError(f"{name} is not a valid timestamp")
    timestamp = (
        timestamp.tz_localize("UTC")
        if timestamp.tz is None
        else timestamp.tz_convert("UTC")
    )
    if timestamp != timestamp.normalize():
        raise ValueError(f"{name} must be UTC midnight")
    return timestamp


def months(start: pd.Timestamp, end: pd.Timestamp) -> tuple[str, ...]:
    if end <= start:
        raise ValueError("end must follow start")
    first = start.tz_localize(None).to_period("M")
    final = (end - pd.Timedelta(nanoseconds=1)).tz_localize(None).to_period("M")
    return tuple(str(item) for item in pd.period_range(first, final, freq="M"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_bytes(url: str, *, retries: int) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            last = exc
            if attempt + 1 == retries:
                break
            time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"download failed after {retries} attempts: {url}") from last


def fetch_to_path(url: str, path: Path, *, retries: int) -> None:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=300) as response, path.open(
                "wb"
            ) as output:
                shutil.copyfileobj(response, output, length=8 * 1024 * 1024)
            return
        except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            last = exc
            path.unlink(missing_ok=True)
            if attempt + 1 == retries:
                break
            time.sleep(min(30, 2**attempt))
    raise RuntimeError(f"download failed after {retries} attempts: {url}") from last


def parse_checksum(payload: bytes) -> str:
    token = payload.decode("utf-8-sig").strip().split()[0].lower()
    if len(token) != 64 or any(
        character not in "0123456789abcdef" for character in token
    ):
        raise ValueError(f"invalid SHA-256 payload: {payload[:120]!r}")
    return token


def timestamp_to_ms(values: pd.Series) -> np.ndarray:
    array = pd.to_numeric(values, errors="raise").to_numpy(
        dtype=np.int64, copy=False
    )
    maximum = int(array.max(initial=0))
    if maximum < 10**11:
        return array * 1000
    if maximum < 10**14:
        return array
    if maximum < 10**17:
        return array // 1000
    return array // 1_000_000


def bool_buyer_maker(values: pd.Series) -> np.ndarray:
    if values.dtype == bool:
        return values.to_numpy(dtype=bool, copy=False)
    lowered = values.astype(str).str.strip().str.lower()
    mapped = lowered.map({"true": True, "false": False, "1": True, "0": False})
    if mapped.isna().any():
        bad = sorted(set(lowered[mapped.isna()].head(10)))
        raise ValueError(f"invalid is_buyer_maker values: {bad}")
    return mapped.to_numpy(dtype=bool, copy=False)


def empty_state(size: int) -> dict[str, np.ndarray]:
    shape = (size, 6)
    return {
        "signed": np.zeros(shape, dtype=np.float64),
        "total": np.zeros(shape, dtype=np.float64),
        "buy": np.zeros(shape, dtype=np.float64),
        "sell": np.zeros(shape, dtype=np.float64),
        "base_qty": np.zeros(shape, dtype=np.float64),
        "agg_rows": np.zeros(shape, dtype=np.int64),
        "trade_count": np.zeros(shape, dtype=np.int64),
        "open": np.full(shape, np.nan, dtype=np.float64),
        "high": np.full(shape, np.nan, dtype=np.float64),
        "low": np.full(shape, np.nan, dtype=np.float64),
        "close": np.full(shape, np.nan, dtype=np.float64),
        "first_ts": np.full(shape, -1, dtype=np.int64),
        "last_ts": np.full(shape, -1, dtype=np.int64),
        "boundary_price": np.full(size, np.nan, dtype=np.float64),
        "boundary_ts": np.full(size, -1, dtype=np.int64),
        "entry_price": np.full((size, 3), np.nan, dtype=np.float64),
        "entry_ts": np.full((size, 3), -1, dtype=np.int64),
    }


def update_grouped_bins(
    state: dict[str, np.ndarray],
    indices: np.ndarray,
    bin_indices: np.ndarray,
    timestamps_ms: np.ndarray,
    prices: np.ndarray,
    quantities: np.ndarray,
    quote_values: np.ndarray,
    signed_values: np.ndarray,
    buyer_maker: np.ndarray,
    actual_trade_count: np.ndarray,
) -> None:
    if not len(indices):
        return
    temporary = pd.DataFrame(
        {
            "idx": indices,
            "bin": bin_indices,
            "ts": timestamps_ms,
            "price": prices,
            "qty": quantities,
            "quote": quote_values,
            "signed": signed_values,
            "buy": np.where(buyer_maker, 0.0, quote_values),
            "sell": np.where(buyer_maker, quote_values, 0.0),
            "actual_count": actual_trade_count,
        }
    )
    grouped = temporary.groupby(
        ["idx", "bin"], sort=False, observed=True
    ).agg(
        signed=("signed", "sum"),
        total=("quote", "sum"),
        buy=("buy", "sum"),
        sell=("sell", "sum"),
        base_qty=("qty", "sum"),
        agg_rows=("quote", "size"),
        trade_count=("actual_count", "sum"),
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        first_ts=("ts", "first"),
        last_ts=("ts", "last"),
    )
    group_indices = grouped.index.get_level_values(0).to_numpy(dtype=np.int64)
    group_bins = grouped.index.get_level_values(1).to_numpy(dtype=np.int64)
    for key in (
        "signed",
        "total",
        "buy",
        "sell",
        "base_qty",
        "agg_rows",
        "trade_count",
    ):
        state[key][group_indices, group_bins] += grouped[key].to_numpy()
    old_open = state["open"][group_indices, group_bins]
    take_open = np.isnan(old_open)
    state["open"][
        group_indices[take_open], group_bins[take_open]
    ] = grouped["open"].to_numpy()[take_open]
    state["first_ts"][
        group_indices[take_open], group_bins[take_open]
    ] = grouped["first_ts"].to_numpy(dtype=np.int64)[take_open]
    state["close"][group_indices, group_bins] = grouped["close"].to_numpy()
    state["last_ts"][group_indices, group_bins] = grouped["last_ts"].to_numpy(
        dtype=np.int64
    )
    new_high = grouped["high"].to_numpy()
    new_low = grouped["low"].to_numpy()
    current_high = state["high"][group_indices, group_bins]
    current_low = state["low"][group_indices, group_bins]
    state["high"][group_indices, group_bins] = np.where(
        np.isnan(current_high), new_high, np.maximum(current_high, new_high)
    )
    state["low"][group_indices, group_bins] = np.where(
        np.isnan(current_low), new_low, np.minimum(current_low, new_low)
    )


def update_first_after_threshold(
    output_price: np.ndarray,
    output_timestamp: np.ndarray,
    *,
    indices: np.ndarray,
    offsets: np.ndarray,
    timestamps_ms: np.ndarray,
    prices: np.ndarray,
    threshold_ms: int,
) -> None:
    mask = offsets >= threshold_ms
    if not np.any(mask):
        return
    candidate = pd.DataFrame(
        {"idx": indices[mask], "ts": timestamps_ms[mask], "price": prices[mask]}
    )
    first = candidate.groupby("idx", sort=False, observed=True).first()
    group_indices = first.index.to_numpy(dtype=np.int64)
    empty = output_timestamp[group_indices] < 0
    if not np.any(empty):
        return
    take = group_indices[empty]
    output_timestamp[take] = first.loc[take, "ts"].to_numpy(dtype=np.int64)
    output_price[take] = first.loc[take, "price"].to_numpy(dtype=np.float64)


def process_csv_member(
    handle: io.BufferedReader,
    *,
    state: dict[str, np.ndarray],
    start_ms: int,
    end_ms: int,
    chunk_rows: int,
    previous_id: int | None,
    previous_timestamp: int | None,
) -> tuple[int, int, int, int, int]:
    total_rows = 0
    minimum_timestamp = 2**63 - 1
    maximum_timestamp = -1
    last_id_seen = previous_id
    last_timestamp_seen = previous_timestamp
    reader = pd.read_csv(
        handle, header=None, chunksize=chunk_rows, low_memory=False
    )
    for raw in reader:
        if raw.shape[1] < len(COLS):
            raise ValueError(
                f"aggTrades CSV has {raw.shape[1]} columns; expected at least {len(COLS)}"
            )
        raw = raw.iloc[:, : len(COLS)].copy()
        raw.columns = COLS
        numeric_id = pd.to_numeric(raw["agg_trade_id"], errors="coerce")
        if numeric_id.isna().iloc[0]:
            raw = raw.iloc[1:].copy()
            if raw.empty:
                continue
            numeric_id = pd.to_numeric(raw["agg_trade_id"], errors="raise")
        elif numeric_id.isna().any():
            raise ValueError("non-numeric aggregate trade id inside data rows")
        identifiers = numeric_id.to_numpy(dtype=np.int64, copy=False)
        if len(identifiers) > 1 and np.any(identifiers[1:] <= identifiers[:-1]):
            raise ValueError(
                "aggregate trade ids are not strictly increasing within chunk"
            )
        if last_id_seen is not None and int(identifiers[0]) <= last_id_seen:
            raise ValueError(
                "aggregate trade ids reverse or duplicate across chunks/months"
            )
        last_id_seen = int(identifiers[-1])

        timestamps_ms = timestamp_to_ms(raw["transact_time"])
        if len(timestamps_ms) > 1 and np.any(timestamps_ms[1:] < timestamps_ms[:-1]):
            raise ValueError("trade timestamps reverse within chunk")
        if (
            last_timestamp_seen is not None
            and int(timestamps_ms[0]) < last_timestamp_seen
        ):
            raise ValueError("trade timestamps reverse across chunks/months")
        last_timestamp_seen = int(timestamps_ms[-1])
        minimum_timestamp = min(minimum_timestamp, int(timestamps_ms[0]))
        maximum_timestamp = max(maximum_timestamp, int(timestamps_ms[-1]))
        total_rows += len(raw)

        in_range = (timestamps_ms >= start_ms) & (timestamps_ms < end_ms)
        if not np.any(in_range):
            continue
        timestamps = timestamps_ms[in_range]
        prices = pd.to_numeric(
            raw.loc[in_range, "price"], errors="raise"
        ).to_numpy(dtype=np.float64)
        quantities = pd.to_numeric(
            raw.loc[in_range, "quantity"], errors="raise"
        ).to_numpy(dtype=np.float64)
        first_ids = pd.to_numeric(
            raw.loc[in_range, "first_trade_id"], errors="raise"
        ).to_numpy(dtype=np.int64)
        last_ids = pd.to_numeric(
            raw.loc[in_range, "last_trade_id"], errors="raise"
        ).to_numpy(dtype=np.int64)
        actual_count = last_ids - first_ids + 1
        if (
            np.any(actual_count <= 0)
            or np.any(prices <= 0)
            or np.any(quantities <= 0)
        ):
            raise ValueError("non-positive price/quantity/count in aggregate trades")
        maker = bool_buyer_maker(raw.loc[in_range, "is_buyer_maker"])
        quote = prices * quantities
        signed = np.where(maker, -quote, quote)
        quarter_start_ms = (timestamps // 900_000) * 900_000
        indices = ((quarter_start_ms - start_ms) // 900_000).astype(np.int64)
        offsets = timestamps - quarter_start_ms
        state_size = len(state["boundary_ts"])
        if np.any(indices < 0) or np.any(indices >= state_size):
            raise AssertionError("quarter-hour index outside requested range")

        first = pd.DataFrame(
            {"idx": indices, "ts": timestamps, "price": prices}
        ).groupby("idx", sort=False, observed=True).first()
        group_indices = first.index.to_numpy(dtype=np.int64)
        empty = state["boundary_ts"][group_indices] < 0
        if np.any(empty):
            take = group_indices[empty]
            state["boundary_ts"][take] = first.loc[take, "ts"].to_numpy(
                dtype=np.int64
            )
            state["boundary_price"][take] = first.loc[take, "price"].to_numpy(
                dtype=np.float64
            )

        for entry_index, seconds in enumerate(ENTRY_SECONDS):
            update_first_after_threshold(
                state["entry_price"][:, entry_index],
                state["entry_ts"][:, entry_index],
                indices=indices,
                offsets=offsets,
                timestamps_ms=timestamps,
                prices=prices,
                threshold_ms=seconds * 1000,
            )

        first_minute = offsets < 60_000
        if np.any(first_minute):
            bins = (offsets[first_minute] // 10_000).astype(np.int64)
            update_grouped_bins(
                state,
                indices[first_minute],
                bins,
                timestamps[first_minute],
                prices[first_minute],
                quantities[first_minute],
                quote[first_minute],
                signed[first_minute],
                maker[first_minute],
                actual_count[first_minute],
            )
    if total_rows == 0:
        raise ValueError("aggTrades archive contains no data rows")
    return (
        total_rows,
        minimum_timestamp,
        maximum_timestamp,
        int(last_id_seen),
        int(last_timestamp_seen),
    )


def process_zip(
    path: Path,
    *,
    state: dict[str, np.ndarray],
    start_ms: int,
    end_ms: int,
    chunk_rows: int,
    previous_id: int | None,
    previous_timestamp: int | None,
) -> tuple[dict[str, object], int, int]:
    members_metadata: list[dict[str, object]] = []
    last_id = previous_id
    last_timestamp = previous_timestamp
    with zipfile.ZipFile(path) as archive:
        members = sorted(
            name for name in archive.namelist() if name.lower().endswith(".csv")
        )
        if not members:
            raise ValueError(f"{path.name}: no CSV member")
        for member in members:
            with archive.open(member) as handle:
                (
                    rows,
                    minimum_timestamp,
                    maximum_timestamp,
                    last_id,
                    last_timestamp,
                ) = process_csv_member(
                    handle,
                    state=state,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    chunk_rows=chunk_rows,
                    previous_id=last_id,
                    previous_timestamp=last_timestamp,
                )
            members_metadata.append(
                {
                    "member": member,
                    "rows": rows,
                    "min_timestamp_ms": minimum_timestamp,
                    "max_timestamp_ms": maximum_timestamp,
                    "last_agg_trade_id": last_id,
                }
            )
    return {"members": members_metadata}, int(last_id), int(last_timestamp)


def cumulative_ohlc(
    state: dict[str, np.ndarray], bins: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    opens = state["open"][:, :bins]
    highs = state["high"][:, :bins]
    lows = state["low"][:, :bins]
    closes = state["close"][:, :bins]
    size = opens.shape[0]
    output_open = np.full(size, np.nan)
    output_close = np.full(size, np.nan)
    for bin_index in range(bins):
        take_open = np.isnan(output_open) & ~np.isnan(opens[:, bin_index])
        output_open[take_open] = opens[take_open, bin_index]
        take_close = ~np.isnan(closes[:, bin_index])
        output_close[take_close] = closes[take_close, bin_index]
    with np.errstate(all="ignore"):
        output_high = np.nanmax(highs, axis=1)
        output_low = np.nanmin(lows, axis=1)
    all_nan = np.isnan(highs).all(axis=1)
    output_high[all_nan] = np.nan
    output_low[all_nan] = np.nan
    return output_open, output_high, output_low, output_close


def build_output(
    state: dict[str, np.ndarray],
    start: pd.Timestamp,
    end: pd.Timestamp,
    symbol: str,
) -> pd.DataFrame:
    clock = pd.date_range(start, end, freq="15min", inclusive="left")
    output = pd.DataFrame({"timestamp": clock, "symbol": symbol})
    for seconds, bins in ((10, 1), (30, 3), (60, 6)):
        signed = state["signed"][:, :bins].sum(axis=1)
        total = state["total"][:, :bins].sum(axis=1)
        buy = state["buy"][:, :bins].sum(axis=1)
        sell = state["sell"][:, :bins].sum(axis=1)
        base_quantity = state["base_qty"][:, :bins].sum(axis=1)
        aggregate_rows = state["agg_rows"][:, :bins].sum(axis=1)
        trade_count = state["trade_count"][:, :bins].sum(axis=1)
        open_price, high_price, low_price, close_price = cumulative_ohlc(
            state, bins
        )
        output[f"signed_trade_quote_{seconds}s"] = signed
        output[f"total_trade_quote_{seconds}s"] = total
        output[f"aggressive_buy_quote_{seconds}s"] = buy
        output[f"aggressive_sell_quote_{seconds}s"] = sell
        output[f"imbalance_{seconds}s"] = np.divide(
            signed, total, out=np.zeros_like(signed), where=total > 0
        )
        output[f"base_quantity_{seconds}s"] = base_quantity
        output[f"aggregate_rows_{seconds}s"] = aggregate_rows
        output[f"underlying_trade_count_{seconds}s"] = trade_count
        output[f"open_{seconds}s"] = open_price
        output[f"high_{seconds}s"] = high_price
        output[f"low_{seconds}s"] = low_price
        output[f"close_{seconds}s"] = close_price
        output[f"log_return_{seconds}s"] = np.where(
            (~np.isnan(open_price))
            & (~np.isnan(close_price))
            & (open_price > 0)
            & (close_price > 0),
            np.log(close_price / open_price),
            np.nan,
        )
    output["boundary_first_trade_time"] = pd.to_datetime(
        state["boundary_ts"], unit="ms", utc=True, errors="coerce"
    )
    output["boundary_first_trade_price"] = state["boundary_price"]
    start_milliseconds = (
        clock.astype("int64") // 1_000_000
    ).to_numpy(dtype=np.int64)
    for entry_index, seconds in enumerate(ENTRY_SECONDS):
        output[f"entry_after_{seconds}s_time"] = pd.to_datetime(
            state["entry_ts"][:, entry_index],
            unit="ms",
            utc=True,
            errors="coerce",
        )
        output[f"entry_after_{seconds}s_price"] = state["entry_price"][
            :, entry_index
        ]
        delay = state["entry_ts"][:, entry_index] - start_milliseconds
        output[f"entry_after_{seconds}s_delay_ms"] = np.where(
            state["entry_ts"][:, entry_index] >= 0, delay, np.nan
        )
    return output


def main() -> int:
    args = parse_args()
    symbol = args.symbol.strip().upper()
    if not symbol or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        for character in symbol
    ):
        raise ValueError("invalid symbol")
    start = utc_midnight(args.start, name="start")
    end = utc_midnight(args.end, name="end")
    if end <= start:
        raise ValueError("end must follow start")
    clock = pd.date_range(start, end, freq="15min", inclusive="left")
    state = empty_state(len(clock))
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    source_metadata: list[dict[str, object]] = []
    previous_id: int | None = None
    previous_timestamp: int | None = None

    with tempfile.TemporaryDirectory(prefix="wave39-") as temporary_directory:
        temporary = Path(temporary_directory)
        for month in months(start, end):
            archive_name = f"{symbol}-aggTrades-{month}.zip"
            url = f"{ARCHIVE_ROOT}/{symbol}/{archive_name}"
            checksum_url = f"{url}.CHECKSUM"
            print(json.dumps({"event": "download", "url": url}), flush=True)
            expected = parse_checksum(
                fetch_bytes(checksum_url, retries=args.retries)
            )
            archive_path = temporary / archive_name
            fetch_to_path(url, archive_path, retries=args.retries)
            actual = sha256_file(archive_path)
            if actual != expected:
                raise ValueError(
                    f"checksum mismatch for {archive_name}: {actual} != {expected}"
                )
            archive_metadata, previous_id, previous_timestamp = process_zip(
                archive_path,
                state=state,
                start_ms=start_ms,
                end_ms=end_ms,
                chunk_rows=args.chunk_rows,
                previous_id=previous_id,
                previous_timestamp=previous_timestamp,
            )
            source_metadata.append(
                {
                    "month": month,
                    "url": url,
                    "checksum_url": checksum_url,
                    "sha256": actual,
                    "bytes": archive_path.stat().st_size,
                    **archive_metadata,
                }
            )
            archive_path.unlink()

    output = build_output(state, start, end, symbol)
    if len(output) != len(clock) or output["timestamp"].duplicated().any():
        raise AssertionError("invalid output clock")
    if not output["timestamp"].is_monotonic_increasing:
        raise AssertionError("output clock is not increasing")
    stem = (
        f"{symbol}-quarterhour-exact-flow-"
        f"{start:%Y%m%d}-{end:%Y%m%d}"
    )
    parquet_path = args.output_dir / f"{stem}.parquet"
    output.to_parquet(parquet_path, index=False, compression="zstd")
    manifest = {
        "schema_version": 1,
        "source": "official Binance USD-M monthly aggTrades archives",
        "symbol": symbol,
        "start_inclusive": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "quarter_rows": int(len(output)),
        "zero_flow_10s_rows": int(
            (output["total_trade_quote_10s"] == 0).sum()
        ),
        "missing_entry_after_10s": int(
            output["entry_after_10s_price"].isna().sum()
        ),
        "missing_entry_after_30s": int(
            output["entry_after_30s_price"].isna().sum()
        ),
        "missing_entry_after_60s": int(
            output["entry_after_60s_price"].isna().sum()
        ),
        "output": {
            "path": parquet_path.name,
            "bytes": parquet_path.stat().st_size,
            "sha256": sha256_file(parquet_path),
        },
        "source_archives": source_metadata,
        "causality": {
            "signal_windows": (
                "[quarter_hour, quarter_hour + 10/30/60 seconds)"
            ),
            "entry_prices": (
                "first observed aggregate trade at or after the respective "
                "completed signal window"
            ),
            "buyer_maker_semantics": (
                "buyer_is_maker=true is seller-aggressor and receives "
                "negative signed quote"
            ),
            "no_missing_trade_imputation": True,
        },
    }
    manifest_path = args.output_dir / f"{stem}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

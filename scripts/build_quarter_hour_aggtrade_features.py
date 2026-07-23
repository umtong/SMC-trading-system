from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

BASE = 'https://data.binance.vision/data/futures/um/monthly/aggTrades'
SHA_RE = re.compile(r'\b([0-9a-fA-F]{64})\b')
COLS = (
    'agg_trade_id',
    'price',
    'quantity',
    'first_trade_id',
    'last_trade_id',
    'transact_time',
    'is_buyer_maker',
)
QUARTER_MS = 15 * 60 * 1000
WINDOW_MS = 10 * 1000


@dataclass
class EventAccumulator:
    boundary_ms: int
    prior_last_time_ms: int | None = None
    prior_last_price: float | None = None
    open_first_time_ms: int | None = None
    open_first_price: float | None = None
    open_last_time_ms: int | None = None
    open_last_price: float | None = None
    total_qty: float = 0.0
    signed_qty: float = 0.0
    total_quote: float = 0.0
    signed_quote: float = 0.0
    buyer_taker_qty: float = 0.0
    seller_taker_qty: float = 0.0
    trade_count: int = 0
    last_aggressor: int = 0

    def prior(self, timestamp_ms: int, price: float) -> None:
        if self.prior_last_time_ms is None or timestamp_ms >= self.prior_last_time_ms:
            self.prior_last_time_ms = timestamp_ms
            self.prior_last_price = price

    def opening(self, timestamp_ms: int, price: float, quantity: float, buyer_maker: bool) -> None:
        side = -1 if buyer_maker else 1
        quote = price * quantity
        if self.open_first_time_ms is None or timestamp_ms < self.open_first_time_ms:
            self.open_first_time_ms = timestamp_ms
            self.open_first_price = price
        if self.open_last_time_ms is None or timestamp_ms >= self.open_last_time_ms:
            self.open_last_time_ms = timestamp_ms
            self.open_last_price = price
            self.last_aggressor = side
        self.total_qty += quantity
        self.signed_qty += side * quantity
        self.total_quote += quote
        self.signed_quote += side * quote
        if side > 0:
            self.buyer_taker_qty += quantity
        else:
            self.seller_taker_qty += quantity
        self.trade_count += 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--end-month', type=int, default=12)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path, required=True)
    parser.add_argument('--chunk-size', type=int, default=1_000_000)
    return parser.parse_args()


def month_sequence(year: int, end_month: int) -> list[tuple[int, int]]:
    if not 1 <= end_month <= 12:
        raise ValueError('end-month must be in 1..12')
    sequence = [(year - 1, 12)]
    sequence.extend((year, month) for month in range(1, end_month + 1))
    return sequence


def get(url: str, attempts: int = 7) -> bytes:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'smc-ict-wave19/1.0'})
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise FileNotFoundError(url) from exc
            error = exc
        except Exception as exc:
            error = exc
        if attempt + 1 < attempts:
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f'download failed after {attempts} attempts: {url}: {error}')


def verified_archive(symbol: str, year: int, month: int, cache_dir: Path) -> tuple[Path, dict[str, object]]:
    month_text = f'{year:04d}-{month:02d}'
    name = f'{symbol}-aggTrades-{month_text}.zip'
    url = f'{BASE}/{symbol}/{name}'
    path = cache_dir / symbol / name
    checksum_path = path.with_suffix(path.suffix + '.CHECKSUM')
    path.parent.mkdir(parents=True, exist_ok=True)

    checksum_payload = get(url + '.CHECKSUM')
    match = SHA_RE.search(checksum_payload.decode('utf-8', errors='strict'))
    if match is None:
        raise ValueError(f'no SHA-256 in {url}.CHECKSUM')
    expected = match.group(1).lower()

    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        disposition = 'cache_verified'
    else:
        payload = get(url)
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected:
            raise ValueError(f'checksum mismatch {url}: {actual} != {expected}')
        path.write_bytes(payload)
        disposition = 'downloaded'
    checksum_path.write_bytes(checksum_payload)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, {
        'symbol': symbol,
        'month': month_text,
        'url': url,
        'official_sha256': expected,
        'actual_sha256': actual,
        'bytes': path.stat().st_size,
        'disposition': disposition,
    }


def detect_header(archive: zipfile.ZipFile, member: str) -> bool:
    with archive.open(member) as raw:
        first = raw.readline().decode('utf-8-sig', errors='strict').strip()
    if not first:
        raise ValueError(f'empty CSV member: {member}')
    token = first.split(',', 1)[0].strip().strip('"')
    try:
        int(float(token))
        return False
    except ValueError:
        return True


def epoch_ms(values: pd.Series) -> np.ndarray:
    numeric = pd.to_numeric(values, errors='raise').to_numpy(np.int64, copy=False)
    result = numeric.copy()
    nano = numeric >= 10**17
    micro = (numeric >= 10**14) & ~nano
    milliseconds = (numeric >= 10**11) & ~micro & ~nano
    seconds = ~milliseconds & ~micro & ~nano
    result[nano] = numeric[nano] // 1_000_000
    result[micro] = numeric[micro] // 1_000
    result[seconds] = numeric[seconds] * 1_000
    return result


def bool_maker(values: pd.Series) -> np.ndarray:
    if pd.api.types.is_bool_dtype(values.dtype):
        return values.to_numpy(bool, copy=False)
    text = values.astype(str).str.strip().str.lower()
    valid = text.isin(('true', 'false', '1', '0'))
    if not bool(valid.all()):
        bad = text.loc[~valid].head().tolist()
        raise ValueError(f'unrecognized is_buyer_maker values: {bad}')
    return text.isin(('true', '1')).to_numpy(bool)


def process_archive(path: Path, events: dict[int, EventAccumulator], chunk_size: int) -> dict[str, int]:
    selected = opening_selected = prior_selected = total_rows = 0
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith('.csv')]
        if len(members) != 1:
            raise ValueError(f'unexpected CSV members in {path}: {members}')
        member = members[0]
        has_header = detect_header(archive, member)
        with archive.open(member) as raw:
            reader = pd.read_csv(
                raw,
                header=0 if has_header else None,
                names=None if has_header else list(COLS),
                usecols=[1, 2, 5, 6],
                chunksize=chunk_size,
                low_memory=False,
            )
            for chunk in reader:
                if len(chunk.columns) != 4:
                    raise ValueError(f'unexpected selected columns in {path}: {chunk.columns}')
                price = pd.to_numeric(chunk.iloc[:, 0], errors='raise').to_numpy(float)
                quantity = pd.to_numeric(chunk.iloc[:, 1], errors='raise').to_numpy(float)
                timestamp = epoch_ms(chunk.iloc[:, 2])
                maker = bool_maker(chunk.iloc[:, 3])
                total_rows += len(chunk)
                if np.any(price <= 0) or np.any(quantity < 0):
                    raise ValueError(f'invalid price/quantity in {path}')
                phase = timestamp % QUARTER_MS
                keep = (phase < WINDOW_MS) | (phase >= QUARTER_MS - WINDOW_MS)
                if not bool(keep.any()):
                    continue
                idx = np.flatnonzero(keep)
                selected += len(idx)
                for i in idx:
                    ts = int(timestamp[i])
                    ph = int(phase[i])
                    px = float(price[i])
                    qty = float(quantity[i])
                    if ph < WINDOW_MS:
                        boundary = ts - ph
                        event = events.setdefault(boundary, EventAccumulator(boundary))
                        event.opening(ts, px, qty, bool(maker[i]))
                        opening_selected += 1
                    else:
                        boundary = ts - ph + QUARTER_MS
                        event = events.setdefault(boundary, EventAccumulator(boundary))
                        event.prior(ts, px)
                        prior_selected += 1
    return {
        'total_rows': total_rows,
        'selected_rows': selected,
        'opening_rows': opening_selected,
        'prior_rows': prior_selected,
    }


def event_rows(events: dict[int, EventAccumulator], year: int, end_month: int) -> list[dict[str, object]]:
    start_ms = int(pd.Timestamp(f'{year:04d}-01-01T00:00:00Z').timestamp() * 1000)
    if end_month == 12:
        end = pd.Timestamp(f'{year + 1:04d}-01-01T00:00:00Z')
    else:
        end = pd.Timestamp(f'{year:04d}-{end_month + 1:02d}-01T00:00:00Z')
    end_ms = int(end.timestamp() * 1000)
    rows: list[dict[str, object]] = []
    for boundary, event in sorted(events.items()):
        if boundary < start_ms or boundary >= end_ms:
            continue
        if event.open_last_price is None or event.total_qty <= 0 or event.total_quote <= 0:
            continue
        prior = event.prior_last_price
        opening_return = math.log(event.open_last_price / prior) if prior is not None and prior > 0 else math.nan
        intrawindow_return = (
            math.log(event.open_last_price / event.open_first_price)
            if event.open_first_price is not None and event.open_first_price > 0
            else math.nan
        )
        rows.append({
            'boundary_time': pd.Timestamp(boundary, unit='ms', tz='UTC').isoformat(),
            'boundary_ms': boundary,
            'prior_last_time_ms': event.prior_last_time_ms,
            'prior_last_price': prior,
            'open_first_time_ms': event.open_first_time_ms,
            'open_first_price': event.open_first_price,
            'open_last_time_ms': event.open_last_time_ms,
            'open_last_price': event.open_last_price,
            'opening_return': opening_return,
            'intrawindow_return': intrawindow_return,
            'total_qty': event.total_qty,
            'signed_qty': event.signed_qty,
            'order_imbalance_qty': event.signed_qty / event.total_qty,
            'total_quote': event.total_quote,
            'signed_quote': event.signed_quote,
            'order_imbalance_quote': event.signed_quote / event.total_quote,
            'buyer_taker_qty': event.buyer_taker_qty,
            'seller_taker_qty': event.seller_taker_qty,
            'trade_count': event.trade_count,
            'last_aggressor': event.last_aggressor,
        })
    return rows


def main() -> int:
    args = parse_args()
    symbol = args.symbol.upper()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    events: dict[int, EventAccumulator] = {}
    archive_records: list[dict[str, object]] = []
    processing_records: list[dict[str, object]] = []
    for year, month in month_sequence(args.year, args.end_month):
        try:
            path, archive_record = verified_archive(symbol, year, month, args.cache_dir)
        except FileNotFoundError:
            if year == args.year:
                raise
            archive_records.append({
                'symbol': symbol,
                'month': f'{year:04d}-{month:02d}',
                'status': 'missing_previous_context_month',
            })
            continue
        stats = process_archive(path, events, args.chunk_size)
        archive_record['status'] = 'verified_and_processed'
        archive_records.append(archive_record)
        processing_records.append({'month': archive_record['month'], **stats})
        print(symbol, archive_record['month'], stats, flush=True)
        path.unlink(missing_ok=True)
        path.with_suffix(path.suffix + '.CHECKSUM').unlink(missing_ok=True)

    rows = event_rows(events, args.year, args.end_month)
    if not rows:
        raise RuntimeError(f'no quarter-hour events produced for {symbol} {args.year}')
    frame = pd.DataFrame(rows)
    frame.insert(0, 'symbol', symbol)
    frame['boundary_time'] = pd.to_datetime(frame.boundary_time, utc=True)
    frame = frame.sort_values('boundary_time').reset_index(drop=True)
    expected = pd.date_range(
        f'{args.year:04d}-01-01T00:00:00Z',
        f'{args.year + 1:04d}-01-01T00:00:00Z' if args.end_month == 12 else f'{args.year:04d}-{args.end_month + 1:02d}-01T00:00:00Z',
        freq='15min',
        inclusive='left',
    )
    missing_events = expected.difference(pd.DatetimeIndex(frame.boundary_time))
    duplicate_events = int(frame.boundary_time.duplicated().sum())
    if duplicate_events:
        raise RuntimeError(f'duplicate quarter-hour events: {duplicate_events}')

    output = args.output_dir / f'{symbol}_quarter_hour_{args.year}.csv.gz'
    frame.to_csv(output, index=False, compression='gzip')
    manifest = {
        'source': 'Binance Vision USD-M monthly aggTrades archives',
        'symbol': symbol,
        'year': args.year,
        'end_month': args.end_month,
        'first_boundary': frame.boundary_time.iloc[0].isoformat(),
        'last_boundary': frame.boundary_time.iloc[-1].isoformat(),
        'events': len(frame),
        'expected_events': len(expected),
        'missing_event_count': len(missing_events),
        'missing_event_examples': [item.isoformat() for item in missing_events[:20]],
        'duplicate_event_count': duplicate_events,
        'events_without_prior_price': int(frame.prior_last_price.isna().sum()),
        'events_without_opening_return': int(frame.opening_return.isna().sum()),
        'output_file': output.name,
        'output_bytes': output.stat().st_size,
        'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'archive_records': archive_records,
        'processing_records': processing_records,
    }
    manifest_path = args.output_dir / f'{symbol}_quarter_hour_{args.year}_manifest.json'
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in manifest.items() if key not in ('archive_records', 'processing_records')}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

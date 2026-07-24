from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

BASE_URL = 'https://data.binance.vision/data/futures/um'
COLUMNS = [
    'open_time','open','high','low','close','volume','close_time','quote_volume',
    'trades','taker_buy_base','taker_buy_quote','ignore',
]

@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    symbol: str
    cadence: str
    period: str
    @property
    def filename(self) -> str:
        return f'{self.symbol}-1m-{self.period}.zip'
    @property
    def url(self) -> str:
        return f'{BASE_URL}/{self.cadence}/klines/{self.symbol}/1m/{self.filename}'

def iter_months(start: str, end: str) -> Iterable[str]:
    year, month = map(int, start.split('-')); ey, em = map(int, end.split('-'))
    while (year, month) <= (ey, em):
        yield f'{year:04d}-{month:02d}'
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)

def iter_days(start: str, end: str) -> Iterable[str]:
    current = date.fromisoformat(start); stop = date.fromisoformat(end)
    while current <= stop:
        yield current.isoformat(); current += timedelta(days=1)

def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()

def parse_checksum(text: str) -> str:
    match = re.search(r'\b([0-9a-fA-F]{64})\b', text)
    if match is None: raise ValueError(f'invalid checksum payload: {text[:120]!r}')
    return match.group(1).lower()

def fetch(spec: ArchiveSpec, *, timeout: int = 120, retries: int = 5):
    headers = {'User-Agent': 'smc-ict-btc-eth-1m/1.0'}; last = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(spec.url, headers=headers, timeout=timeout)
            if response.status_code == 404: raise FileNotFoundError(spec.url)
            response.raise_for_status(); blob = response.content
            checksum = requests.get(spec.url + '.CHECKSUM', headers=headers, timeout=timeout)
            checksum.raise_for_status(); expected = parse_checksum(checksum.text); actual = sha256_bytes(blob)
            if actual != expected: raise ValueError(f'checksum mismatch {spec.filename}: {actual} != {expected}')
            return spec, blob, {'url': spec.url, 'filename': spec.filename, 'bytes': len(blob), 'sha256': actual, 'attempt': attempt}
        except FileNotFoundError: raise
        except Exception as exc:
            last = exc; time.sleep(min(2 ** attempt, 20))
    assert last is not None; raise last

def timestamp_unit(values: pd.Series) -> str:
    numeric = pd.to_numeric(values, errors='raise')
    return 'us' if numeric.abs().median() > 10**14 else 'ms'

def parse_zip(blob: bytes, spec: ArchiveSpec) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith('.csv')]
        if len(names) != 1: raise ValueError(f'{spec.filename}: expected one CSV, got {names}')
        raw = archive.read(names[0])
    first = raw.splitlines()[0].decode('utf-8', errors='replace').lower()
    frame = pd.read_csv(io.BytesIO(raw), header=0 if 'open_time' in first else None)
    if frame.shape[1] < 12: raise ValueError(f'{spec.filename}: expected 12 columns, got {frame.shape[1]}')
    frame = frame.iloc[:, :12].copy(); frame.columns = COLUMNS
    for name in COLUMNS: frame[name] = pd.to_numeric(frame[name], errors='raise')
    unit = timestamp_unit(frame.open_time)
    frame['open_time'] = pd.to_datetime(frame.open_time, unit=unit, utc=True)
    frame['close_time'] = pd.to_datetime(frame.close_time, unit=unit, utc=True)
    frame['symbol'] = spec.symbol; frame['source_cadence'] = spec.cadence; frame['source_period'] = spec.period
    return frame

def validate(frame: pd.DataFrame) -> dict[str, object]:
    ordered = frame.sort_values('open_time'); duplicates = int(ordered.open_time.duplicated().sum())
    if duplicates: raise ValueError(f'duplicate timestamps: {duplicates}')
    bad = int(((ordered.high < ordered[['open','low','close']].max(axis=1)) | (ordered.low > ordered[['open','high','close']].min(axis=1)) | (ordered[['open','high','low','close']] <= 0).any(axis=1) | (ordered.volume < 0)).sum())
    if bad: raise ValueError(f'invalid OHLCV rows: {bad}')
    deltas = ordered.open_time.diff().dropna(); irregular = deltas[deltas != pd.Timedelta(minutes=1)]
    gaps = [{'previous': ordered.iloc[i-1].open_time.isoformat(), 'next': ordered.iloc[i].open_time.isoformat(), 'seconds': float(delta.total_seconds())} for i, delta in zip(irregular.index[:100], irregular.iloc[:100])]
    return {'rows': int(len(ordered)), 'start': ordered.open_time.min().isoformat(), 'end': ordered.open_time.max().isoformat(), 'duplicates': duplicates, 'irregular_intervals': int(len(irregular)), 'gap_sample': gaps, 'zero_volume_rows': int((ordered.volume == 0).sum())}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbols', nargs='+', default=['BTCUSDT','ETHUSDT'])
    parser.add_argument('--start-month', default='2020-01')
    parser.add_argument('--end-month', default='2026-06')
    parser.add_argument('--daily-start', default='2026-07-01')
    parser.add_argument('--daily-end', default='2026-07-22')
    parser.add_argument('--workers', type=int, default=12)
    parser.add_argument('--output', type=Path, default=Path('artifacts/binance_usdm_btc_eth_1m'))
    args = parser.parse_args(); args.output.mkdir(parents=True, exist_ok=True)
    specs = [ArchiveSpec(symbol, 'monthly', period) for symbol in args.symbols for period in iter_months(args.start_month, args.end_month)]
    specs += [ArchiveSpec(symbol, 'daily', period) for symbol in args.symbols for period in iter_days(args.daily_start, args.daily_end)]
    chunks: dict[str, list[pd.DataFrame]] = {}; archives = []; missing = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, spec): spec for spec in specs}
        for ordinal, future in enumerate(concurrent.futures.as_completed(futures), 1):
            spec = futures[future]
            try: returned, blob, metadata = future.result()
            except FileNotFoundError: missing.append(spec.url); continue
            chunks.setdefault(returned.symbol, []).append(parse_zip(blob, returned)); archives.append({**asdict(returned), **metadata})
            if ordinal % 25 == 0: print(f'processed {ordinal}/{len(specs)}', flush=True)
    datasets = {}
    for symbol, parts in sorted(chunks.items()):
        frame = pd.concat(parts, ignore_index=True).sort_values('open_time').drop_duplicates('open_time', keep='last').reset_index(drop=True)
        target = args.output / f'{symbol}_1m.parquet'; frame.to_parquet(target, index=False, compression='zstd')
        datasets[symbol] = {**validate(frame), 'path': target.name, 'bytes': target.stat().st_size, 'sha256': sha256_file(target)}
        del frame
    payload = {'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'source': BASE_URL, 'symbols': args.symbols, 'start_month': args.start_month, 'end_month': args.end_month, 'daily_start': args.daily_start, 'daily_end': args.daily_end, 'requested_archives': len(specs), 'verified_archives': len(archives), 'missing_archives': sorted(missing), 'archives': sorted(archives, key=lambda x: (x['symbol'], x['cadence'], x['period'])), 'datasets': datasets}
    (args.output / 'manifest.json').write_text(json.dumps(payload, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'requested': len(specs), 'verified': len(archives), 'missing': len(missing), 'datasets': {k: v['rows'] for k,v in datasets.items()}}, indent=2), flush=True)
    if set(args.symbols) - set(datasets): raise SystemExit('one or more symbols produced no dataset')
    return 0

if __name__ == '__main__': raise SystemExit(main())

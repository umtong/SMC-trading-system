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

BASE_URL = 'https://data.binance.vision/data/futures/um/daily/metrics'
METRIC_COLUMNS = [
    'create_time',
    'symbol',
    'sum_open_interest',
    'sum_open_interest_value',
    'count_toptrader_long_short_ratio',
    'sum_toptrader_long_short_ratio',
    'count_long_short_ratio',
    'sum_taker_long_short_vol_ratio',
]


@dataclass(frozen=True)
class Spec:
    symbol: str
    day: str

    @property
    def filename(self) -> str:
        return f'{self.symbol}-metrics-{self.day}.zip'

    @property
    def url(self) -> str:
        return f'{BASE_URL}/{self.symbol}/{self.filename}'


def iter_days(start: str, end: str) -> Iterable[str]:
    current = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while current <= stop:
        yield current.isoformat()
        current += timedelta(days=1)


def sha256_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def parse_checksum(text: str) -> str:
    match = re.search(r'\b([0-9a-fA-F]{64})\b', text)
    if match is None:
        raise ValueError(f'invalid CHECKSUM payload: {text[:120]!r}')
    return match.group(1).lower()


def fetch_one(spec: Spec, *, timeout: int = 120, retries: int = 5):
    headers = {'User-Agent': 'smc-ict-metrics-research/1.0'}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(spec.url, headers=headers, timeout=timeout)
            if response.status_code == 404:
                raise FileNotFoundError(spec.url)
            response.raise_for_status()
            blob = response.content
            checksum_response = requests.get(
                spec.url + '.CHECKSUM', headers=headers, timeout=timeout
            )
            checksum_response.raise_for_status()
            expected = parse_checksum(checksum_response.text)
            actual = sha256_bytes(blob)
            if actual != expected:
                raise ValueError(
                    f'checksum mismatch for {spec.filename}: {actual} != {expected}'
                )
            return spec, blob, {
                'url': spec.url,
                'filename': spec.filename,
                'bytes': len(blob),
                'sha256': actual,
                'attempt': attempt,
            }
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(min(2**attempt, 20))
    if last_error is None:
        raise RuntimeError('retry loop ended without an error')
    raise last_error


def timestamp_unit(values: pd.Series) -> str:
    numeric = pd.to_numeric(values, errors='raise')
    median = float(numeric.abs().median())
    if median > 10**14:
        return 'us'
    if median > 10**11:
        return 'ms'
    return 's'


def read_zip(blob: bytes, spec: Spec) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith('.csv')]
        if len(names) != 1:
            raise ValueError(f'{spec.filename}: expected one CSV, found {names}')
        raw = archive.read(names[0])
    frame = pd.read_csv(io.BytesIO(raw))
    lower = {str(c).strip().lower(): c for c in frame.columns}
    missing = [c for c in METRIC_COLUMNS if c not in lower]
    if missing:
        headerless = pd.read_csv(io.BytesIO(raw), header=None)
        if headerless.shape[1] != len(METRIC_COLUMNS):
            raise ValueError(
                f'{spec.filename}: missing {missing}; columns={frame.columns.tolist()}'
            )
        headerless.columns = METRIC_COLUMNS
        frame = headerless
    else:
        frame = frame[[lower[c] for c in METRIC_COLUMNS]].copy()
        frame.columns = METRIC_COLUMNS
    times = pd.to_numeric(frame['create_time'], errors='raise')
    frame['create_time'] = pd.to_datetime(
        times, unit=timestamp_unit(times), utc=True
    )
    frame['symbol'] = frame['symbol'].astype(str).str.upper().str.strip()
    for column in METRIC_COLUMNS[2:]:
        frame[column] = pd.to_numeric(frame[column], errors='coerce')
    if not frame['symbol'].eq(spec.symbol).all():
        raise ValueError(f'{spec.filename}: unexpected symbol values')
    return frame


def validate(frame: pd.DataFrame) -> dict[str, object]:
    ordered = frame.sort_values('create_time')
    duplicates = int(ordered['create_time'].duplicated().sum())
    if duplicates:
        raise ValueError(f'duplicate timestamps: {duplicates}')
    delta = ordered['create_time'].diff().dropna()
    irregular = delta[delta != pd.Timedelta(minutes=5)]
    bad = int((ordered[METRIC_COLUMNS[2:]] < 0).any(axis=1).sum())
    if bad:
        raise ValueError(f'negative metric rows: {bad}')
    return {
        'rows': int(len(ordered)),
        'start': ordered['create_time'].min().isoformat(),
        'end': ordered['create_time'].max().isoformat(),
        'duplicates': duplicates,
        'irregular_intervals': int(len(irregular)),
        'gap_sample': [
            {
                'previous': ordered.iloc[int(i) - 1]['create_time'].isoformat(),
                'next': ordered.iloc[int(i)]['create_time'].isoformat(),
                'delta_seconds': float(d.total_seconds()),
            }
            for i, d in zip(irregular.index[:50], irregular.iloc[:50])
            if int(i) > 0
        ],
        'missing_values': {
            c: int(ordered[c].isna().sum()) for c in METRIC_COLUMNS[2:]
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--symbols', nargs='+', default=['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT']
    )
    parser.add_argument('--start', default='2020-09-01')
    parser.add_argument('--end', default='2026-07-21')
    parser.add_argument('--workers', type=int, default=32)
    parser.add_argument(
        '--output', type=Path, default=Path('artifacts/binance_usdm_metrics')
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    specs = [Spec(symbol, day) for symbol in args.symbols for day in iter_days(args.start, args.end)]
    frames: dict[str, list[pd.DataFrame]] = {symbol: [] for symbol in args.symbols}
    archives: list[dict[str, object]] = []
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_one, spec): spec for spec in specs}
        for ordinal, future in enumerate(concurrent.futures.as_completed(futures), 1):
            spec = futures[future]
            try:
                returned, blob, metadata = future.result()
                frames[returned.symbol].append(read_zip(blob, returned))
                archives.append({**asdict(returned), **metadata})
            except FileNotFoundError:
                missing.append(spec.url)
            except Exception as exc:  # noqa: BLE001
                errors.append({'url': spec.url, 'error': repr(exc)})
            if ordinal % 250 == 0:
                print(
                    f'processed {ordinal}/{len(specs)} '
                    f'archives={len(archives)} missing={len(missing)} errors={len(errors)}',
                    flush=True,
                )
    if errors:
        raise RuntimeError(f'metric archive errors: {errors[:10]} (total={len(errors)})')
    datasets: dict[str, dict[str, object]] = {}
    for symbol in args.symbols:
        if not frames[symbol]:
            raise RuntimeError(f'no metric archives for {symbol}')
        combined = (
            pd.concat(frames[symbol], ignore_index=True)
            .sort_values('create_time')
            .drop_duplicates('create_time', keep='last')
            .reset_index(drop=True)
        )
        target = args.output / f'{symbol}_metrics.csv.gz'
        combined.to_csv(
            target,
            index=False,
            compression={'method': 'gzip', 'compresslevel': 6, 'mtime': 0},
            float_format='%.12g',
        )
        datasets[symbol] = {
            'path': target.name,
            'bytes': target.stat().st_size,
            'sha256': sha256_file(target),
            **validate(combined),
        }
    manifest = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source': BASE_URL,
        'symbols': args.symbols,
        'start': args.start,
        'end': args.end,
        'archives': sorted(archives, key=lambda x: (str(x['symbol']), str(x['day']))),
        'missing_archives': sorted(missing),
        'datasets': datasets,
    }
    (args.output / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print(
        json.dumps(
            {
                'archives': len(archives),
                'missing': len(missing),
                'datasets': datasets,
            },
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

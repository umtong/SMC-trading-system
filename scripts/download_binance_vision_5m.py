from __future__ import annotations

import argparse
import json
from pathlib import Path

from ictbt.easychart_v0.binance_vision import download_symbol, intervals_from_manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download checksum-verified Binance Vision USDT-M 5m klines."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", default=("BTCUSDT", "ETHUSDT"))
    parser.add_argument("--interval", default="5m", choices=("5m",))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/binance_vision"))
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    intervals = intervals_from_manifest(payload)
    outputs = [
        download_symbol(
            symbol=str(symbol).upper(),
            interval=args.interval,
            intervals=intervals,
            cache_dir=args.cache_dir,
            output_dir=args.output_dir,
        )
        for symbol in args.symbols
    ]
    print(
        json.dumps(
            {
                "intervals": [
                    {"start": item.start.isoformat(), "end": item.end.isoformat()}
                    for item in intervals
                ],
                "outputs": [str(path) for path in outputs],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

DATES = ('2023-06-27', '2023-08-30', '2023-11-09', '2023-12-28')
ROOT = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def panel(args: argparse.Namespace) -> int:
    streaming = load_module('state_first_l1_streaming_v3_available', ROOT / 'state_first_l1_streaming_v3.py')
    streaming.DAYS = DATES
    delegated = argparse.Namespace(
        symbol=args.symbol,
        dates=list(DATES),
        data_dir=args.data_dir,
        output_dir=args.output_dir,
    )
    return int(streaming.panel_command(delegated))


def evaluate(args: argparse.Namespace) -> int:
    fixed = load_module('state_first_l1_strict_available', ROOT / 'run_fixed.py')
    fixed.impl.DAYS = DATES
    delegated = argparse.Namespace(input_dir=args.input_dir, output_dir=args.output_dir)
    return int(fixed._strict_evaluate_command(delegated))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('panel')
    p.add_argument('--symbol', choices=('BTCUSDT', 'ETHUSDT'), required=True)
    p.add_argument('--data-dir', type=Path, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    e = sub.add_parser('evaluate')
    e.add_argument('--input-dir', type=Path, required=True)
    e.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    return panel(args) if args.command == 'panel' else evaluate(args)


if __name__ == '__main__':
    raise SystemExit(main())

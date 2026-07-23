from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time

import pandas as pd

from ictbt.easychart_v0.application import load_5m_csv
from ictbt.easychart_v0.domain import B1Subtype, EntryMode
from ictbt.easychart_v0.pipeline import (
    Opportunity,
    assemble_opportunity as assemble_v03_opportunity,
    build_feature_book,
)
from ictbt.easychart_v0.strategy import SimpleExecutionCosts
from ictbt.easychart_v0.v04 import build_baseline_event_authorities
from ictbt.easychart_v0.v05 import build_m15_m5_liquidity_delivery_result
from ictbt.easychart_v0.v07 import build_v07_scene_family_result
from ictbt.microstructure import adapt_authority_to_dual_clock_scene
from ictbt.microstructure.scene_manifest_v2 import record_from_dual_clock_scene
from scripts.v09_contract import (
    HOLDOUT_END,
    RESEARCH_START,
    SYMBOL_TICKS,
    TARGET_ENTRY_FEE_RATE,
    TARGET_EXIT_FEE_RATE,
    WARMUP_DAYS,
    month_bounds,
    utc,
)


TARGET_COSTS = SimpleExecutionCosts(
    entry_fee_rate=TARGET_ENTRY_FEE_RATE,
    exit_fee_rate=TARGET_EXIT_FEE_RATE,
)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=tuple(SYMBOL_TICKS), required=True)
    parser.add_argument("--month", required=True, help="registered YYYY-MM month")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def _registered_month(value: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    begin, end = month_bounds(f"{value}-01" if len(value) == 7 else value)
    if begin < utc(RESEARCH_START) or end > utc(HOLDOUT_END):
        raise ValueError(f"month {value} is outside the registered V0.9.1 interval")
    return begin, end


def _inside(authority: object, start: pd.Timestamp, end: pd.Timestamp) -> bool:
    known = pd.Timestamp(getattr(authority, "known_at")).tz_convert("UTC")
    return start <= known < end


def _preferred_v03(authorities: tuple[object, ...]) -> tuple[object, ...]:
    grouped: dict[str, list[object]] = {}
    for authority in authorities:
        confirmation = getattr(authority, "confirmation")
        grouped.setdefault(str(getattr(confirmation, "authority_id")), []).append(authority)
    selected = [
        min(
            items,
            key=lambda authority: (
                0 if bool(getattr(authority, "has_literal_body_overlap")) else 1,
                float(getattr(authority, "zone").width),
                -pd.Timestamp(getattr(authority, "known_at")).value,
                str(getattr(authority, "authority_id")),
            ),
        )
        for items in grouped.values()
    ]
    return tuple(
        sorted(
            selected,
            key=lambda authority: (
                pd.Timestamp(getattr(authority, "known_at")),
                str(getattr(authority, "authority_id")),
            ),
        )
    )


def _record(
    authority: object,
    *,
    book: object,
    tick_size: float,
    destination: object | None = None,
):
    return record_from_dual_clock_scene(
        adapt_authority_to_dual_clock_scene(
            authority,
            book=book,
            tick_size=tick_size,
            destination=destination,  # type: ignore[arg-type]
        )
    )


def main() -> int:
    args = _args()
    started = time.perf_counter()
    evaluation_start, evaluation_end = _registered_month(args.month)
    warmup_start = evaluation_start - pd.Timedelta(days=WARMUP_DAYS)
    tick_size = float(SYMBOL_TICKS[args.symbol])
    source_path = args.data_dir / f"{args.symbol}_5m.csv"
    source = load_5m_csv(source_path)
    candles = source.loc[
        (source.index >= warmup_start) & (source.index < evaluation_end)
    ].copy()
    expected = pd.date_range(
        warmup_start,
        evaluation_end,
        freq="5min",
        inclusive="left",
    )
    missing = expected.difference(candles.index)
    if len(missing):
        preview = ", ".join(item.isoformat() for item in missing[:5])
        raise ValueError(
            f"{args.symbol}/{args.month}: missing {len(missing)} 5m bars; first={preview}"
        )
    book = build_feature_book(candles, symbol=args.symbol, tick_size=tick_size)

    raw_v03 = tuple(
        authority
        for authority in build_baseline_event_authorities(book)
        if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
        and _inside(authority, evaluation_start, evaluation_end)
    )
    v03 = _preferred_v03(raw_v03)
    v05_result = build_m15_m5_liquidity_delivery_result(book)
    v05 = tuple(
        authority
        for authority in v05_result.authorities
        if _inside(authority, evaluation_start, evaluation_end)
    )
    v07_result = build_v07_scene_family_result(book)
    v07 = tuple(
        authority
        for authority in v07_result.authorities
        if _inside(authority, evaluation_start, evaluation_end)
    )

    records: list[object] = []
    target_rejections: Counter[str] = Counter()
    adaptation_rejections: Counter[str] = Counter()

    for authority in v03:
        opportunity = assemble_v03_opportunity(
            book,
            authority,
            as_of=authority.known_at,
            costs=TARGET_COSTS,
            event_created_entry_mode=EntryMode.NEXT_BAR_OPEN,
        )
        if not isinstance(opportunity, Opportunity):
            target_rejections[str(getattr(opportunity, "reason", "unknown"))] += 1
            continue
        try:
            records.append(
                _record(
                    authority,
                    book=book,
                    tick_size=tick_size,
                    destination=opportunity.target,
                )
            )
        except ValueError as exc:
            adaptation_rejections[f"v03:{exc}"] += 1

    for family, authorities in (("v05", v05), ("v07", v07)):
        for authority in authorities:
            try:
                records.append(
                    _record(
                        authority,
                        book=book,
                        tick_size=tick_size,
                    )
                )
            except ValueError as exc:
                adaptation_rejections[f"{family}:{exc}"] += 1

    serialized_records = [asdict(record) for record in records]
    kind_counts = Counter(record["kind"] for record in serialized_records)
    family_counts = Counter(record["source_scene_family"] for record in serialized_records)
    payload = {
        "schema_version": 2,
        "contract": "easychart_v091_outcome_blind_dual_clock_scene_month",
        "symbol": args.symbol,
        "month": evaluation_start.strftime("%Y-%m"),
        "evaluation_start": evaluation_start.isoformat(),
        "evaluation_end": evaluation_end.isoformat(),
        "warmup_start": warmup_start.isoformat(),
        "warmup_days": WARMUP_DAYS,
        "tick_size": tick_size,
        "target_costs": {
            "entry_fee_rate": TARGET_ENTRY_FEE_RATE,
            "exit_fee_rate": TARGET_EXIT_FEE_RATE,
        },
        "outcome_blind_selection": True,
        "dual_clock_contract": {
            "event": "actual source liquidity-event or break-bar interval",
            "confirmation": "actual later OB/FVG delivery or acceptance-bar interval",
            "entry": "first one-minute open at confirmation_known_at",
        },
        "diagnostics": {
            "source_5m_rows": int(len(candles)),
            "v03_raw_break_retest_authorities": len(raw_v03),
            "v03_selected_roots": len(v03),
            "v05_authorities": len(v05),
            "v07_authorities": len(v07),
            "target_rejections": dict(target_rejections),
            "adaptation_rejections": dict(adaptation_rejections),
            "family_counts": dict(family_counts),
            "kind_counts": dict(kind_counts),
            "manifest_records": len(serialized_records),
            "elapsed_seconds": time.perf_counter() - started,
        },
        "records": serialized_records,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"{args.symbol}_{evaluation_start.strftime('%Y-%m')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "path": str(path),
                "sha256": digest,
                **payload["diagnostics"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

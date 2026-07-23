from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sys
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_easychart_v08_hardened_random_trials as hardened  # noqa: E402

from ictbt.easychart_v0.domain import B1Subtype  # noqa: E402
from ictbt.easychart_v0.margin import (  # noqa: E402
    MarginSafetyConfig,
    guard_opportunity_margin,
)
from ictbt.easychart_v0.research_contract import (  # noqa: E402
    USER_RISK_FRACTION,
    USER_SYMBOLS,
    USER_YEARS,
    assert_user_research_contract,
    sample_trials_without_year_reuse,
)
from ictbt.easychart_v0.v04 import build_baseline_event_authorities  # noqa: E402
from ictbt.easychart_v0.v05 import (  # noqa: E402
    build_m15_m5_liquidity_delivery_result,
)
from ictbt.easychart_v0.v08_hardened import (  # noqa: E402
    build_v08_hardened_scene_family_result,
)
from ictbt.easychart_v0.v08_integrated import (  # noqa: E402
    build_v08_integrated_intraday_result,
)


ARMS = (
    "leader",
    "leader_plus_v09_hardened_htf",
    "leader_plus_v09_owned_intraday",
    "leader_plus_all_v09",
)
MINIMUM_INDEPENDENT_TRIALS = 20
_DEFAULT_V08_OUTPUT = Path("results/easychart_v08_hardened_random_trials")
_DEFAULT_V09_OUTPUT = Path("results/easychart_v09_random_trials")
_ORIGINAL_ARGS = hardened._args
_ORIGINAL_ASSEMBLE = hardened.base._assemble_candidate
_ORIGINAL_TRADE_ROW = hardened.base._trade_row
_CAPTURED_ARGS = None
_MARGIN_CONFIG = MarginSafetyConfig()


def _strict_args():
    global _CAPTURED_ARGS, _MARGIN_CONFIG
    args = _ORIGINAL_ARGS()
    try:
        assert_user_research_contract(
            risk_fraction=args.risk_fraction,
            score_days_per_year=args.score_days_per_year,
            years=USER_YEARS,
            symbols=USER_SYMBOLS,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.trials < MINIMUM_INDEPENDENT_TRIALS:
        raise SystemExit(
            f"trials must be at least {MINIMUM_INDEPENDENT_TRIALS}"
        )
    if args.maximum_notional_to_equity > 8.0 + 1e-12:
        raise SystemExit("maximum_notional_to_equity cannot exceed 8.0")
    if args.output_dir == _DEFAULT_V08_OUTPUT:
        args.output_dir = _DEFAULT_V09_OUTPUT
    _MARGIN_CONFIG = MarginSafetyConfig(
        execution_leverage=8.0,
        maximum_notional_to_equity=args.maximum_notional_to_equity,
        maintenance_margin_fraction=0.01,
        liquidation_fee_fraction=0.005,
        minimum_stop_to_liquidation_r=1.0,
    )
    _CAPTURED_ARGS = args
    return args


def _priority(authority: object) -> tuple[object, ...]:
    authority_id = str(getattr(authority, "authority_id"))
    family_rank = (
        0
        if authority_id.startswith("v09-")
        else 1
        if authority_id.startswith("v08-hardened-")
        else 2
        if authority_id.startswith("v08-")
        else 3
    )
    return (
        family_rank,
        0 if bool(getattr(authority, "has_literal_body_overlap", False)) else 1,
        float(getattr(authority, "zone").width),
        -getattr(authority, "known_at").value,
        authority_id,
    )


def _deduplicate_authorities(
    authorities: Iterable[object],
) -> tuple[object, ...]:
    grouped: dict[tuple[object, ...], list[object]] = {}
    for authority in authorities:
        key = hardened.base._semantic_key(authority)
        grouped.setdefault(key, []).append(authority)
    selected = [min(items, key=_priority) for items in grouped.values()]
    return tuple(
        sorted(
            selected,
            key=lambda authority: (
                getattr(authority, "known_at"),
                getattr(authority, "authority_id"),
            ),
        )
    )


def _build_authority_sets(
    book,
    *,
    v08_policy,
    intraday_policy,
):
    baseline_break_retest = tuple(
        authority
        for authority in build_baseline_event_authorities(book)
        if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
    )
    v05 = build_m15_m5_liquidity_delivery_result(book)
    leader = _deduplicate_authorities(
        (*baseline_break_retest, *v05.authorities)
    )
    htf = build_v08_hardened_scene_family_result(
        book,
        costs=hardened.base.COSTS,
        policy=v08_policy,
    )
    intraday = build_v08_integrated_intraday_result(
        book,
        costs=hardened.base.COSTS,
        policy=intraday_policy,
    )
    sets = {
        "leader": leader,
        "leader_plus_v09_hardened_htf": _deduplicate_authorities(
            (*leader, *htf.authorities)
        ),
        "leader_plus_v09_owned_intraday": _deduplicate_authorities(
            (*leader, *intraday.authorities)
        ),
        "leader_plus_all_v09": _deduplicate_authorities(
            (*leader, *htf.authorities, *intraday.authorities)
        ),
    }
    diagnostics = {
        "v05": asdict(v05.diagnostics),
        "v09_hardened_htf": asdict(htf.diagnostics),
        "v09_owned_intraday": asdict(intraday.diagnostics),
        "authority_counts": {name: len(items) for name, items in sets.items()},
    }
    return sets, diagnostics


def _assemble_candidate(book, authority, costs):
    result = _ORIGINAL_ASSEMBLE(book, authority, costs)
    return guard_opportunity_margin(
        result,
        costs=costs,
        risk_fraction=USER_RISK_FRACTION,
        config=_MARGIN_CONFIG,
    )


def _trade_row(*, arm, trial_fingerprint, closed):
    row = _ORIGINAL_TRADE_ROW(
        arm=arm,
        trial_fingerprint=trial_fingerprint,
        closed=closed,
    )
    authority_id = str(closed.authority.authority_id)
    if authority_id.startswith("v09-owned-internal-liquidity:"):
        row["source_strategy"] = "v09_owned_intraday"
    elif authority_id.startswith("v08-hardened-htf-liquidity-delivery:"):
        row["source_strategy"] = "v09_hardened_htf"
    return row


def _annotate_summary() -> None:
    if _CAPTURED_ARGS is None:
        return
    path = _CAPTURED_ARGS.output_dir / "summary.json"
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    contract = payload.setdefault("contract", {})
    contract.update(
        {
            "risk_fraction_locked": True,
            "minimum_independent_trials": MINIMUM_INDEPENDENT_TRIALS,
            "per_year_start_date_reuse_within_batch": False,
            "window_overlap_note": (
                "exact year/start pairs are unique; partial overlap is measured "
                "and reported by the inherited governance layer"
            ),
            "terminal_target_ownership": (
                "H1/H4 pre-event external pivot, or M15 pre-event equal-level "
                "liquidity pool; a lone M15 pivot cannot own terminal exit"
            ),
            "target_path_rule": (
                "reject farther liquidity when an active M15/H1/H4 pivot, OB, "
                "or FVG is encountered first"
            ),
            "target_room_measure": "cost-inclusive net R",
            "margin_safety": asdict(_MARGIN_CONFIG),
            "margin_model_boundary": (
                "conservative ENGINEERING_V0 isolated-linear estimate; live must "
                "refresh symbol/notional maintenance brackets and mark-price "
                "rules immediately before order submission"
            ),
            "paper_live_authority": "RESEARCH_ONLY",
        }
    )
    payload["v09_strategy_chain"] = [
        "pre-existing liquidity cause",
        "HTF-compatible M15 location",
        "prompt M5 sweep/reclaim and displacement ownership",
        "first-return executable OB/FVG array",
        "structural invalidation",
        "independently owned and unobstructed draw on liquidity",
        "cost-inclusive target room",
        "fixed 3% sizing with notional and liquidation buffer admission",
    ]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    hardened.ARMS = ARMS
    hardened._args = _strict_args
    hardened._build_authority_sets = _build_authority_sets
    hardened.base.sample_trials = sample_trials_without_year_reuse
    hardened.base._assemble_candidate = _assemble_candidate
    hardened.base._trade_row = _trade_row
    status = int(hardened.main())
    _annotate_summary()
    return status


if __name__ == "__main__":
    raise SystemExit(main())

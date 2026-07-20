from __future__ import annotations

from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import sys

from ictbt.easychart_v0.domain import B1Subtype
from ictbt.easychart_v0.margin import MarginSafetyConfig, guard_opportunity_margin
from ictbt.easychart_v0.research_contract import (
    USER_RISK_FRACTION,
    USER_SYMBOLS,
    USER_YEARS,
    assert_user_research_contract,
    sample_trials_without_year_reuse,
)
from ictbt.easychart_v0.v08_integrated import (
    build_v08_integrated_intraday_result,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_RUNNER_PATH = PROJECT_ROOT / "scripts" / "run_easychart_v08_random_trials.py"
SPEC = importlib.util.spec_from_file_location(
    "run_easychart_v08_random_trials_integrated_base",
    BASE_RUNNER_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load the V0.8 random-trial runner")
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)


_ORIGINAL_ARGS = base._args
_ORIGINAL_ASSEMBLE = base._assemble_candidate
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
    if args.maximum_notional_to_equity > 8.0 + 1e-12:
        raise SystemExit("maximum_notional_to_equity cannot exceed 8.0")
    _MARGIN_CONFIG = MarginSafetyConfig(
        execution_leverage=8.0,
        maximum_notional_to_equity=args.maximum_notional_to_equity,
        maintenance_margin_fraction=0.01,
        liquidation_fee_fraction=0.005,
        minimum_stop_to_liquidation_r=1.0,
    )
    _CAPTURED_ARGS = args
    return args


def _build_authority_sets(
    book,
    *,
    v08_policy,
    intraday_policy,
):
    baseline_break_retest = tuple(
        authority
        for authority in base.build_baseline_event_authorities(book)
        if authority.confirmation.subtype is B1Subtype.BREAK_RETEST
    )
    v05 = base.build_m15_m5_liquidity_delivery_result(book)
    leader = base._deduplicate_authorities(
        (*baseline_break_retest, *v05.authorities)
    )
    v08_htf = base.build_v08_scene_family_result(
        book,
        costs=base.COSTS,
        policy=v08_policy,
    )
    v08_intraday = build_v08_integrated_intraday_result(
        book,
        costs=base.COSTS,
        policy=intraday_policy,
    )
    sets = {
        "leader": leader,
        "leader_plus_v08_htf": base._deduplicate_authorities(
            (*leader, *v08_htf.authorities)
        ),
        "leader_plus_v08_intraday": base._deduplicate_authorities(
            (*leader, *v08_intraday.authorities)
        ),
        "leader_plus_all_v08": base._deduplicate_authorities(
            (*leader, *v08_htf.authorities, *v08_intraday.authorities)
        ),
    }
    diagnostics = {
        "v05": asdict(v05.diagnostics),
        "v08_htf": asdict(v08_htf.diagnostics),
        "v08_intraday": asdict(v08_intraday.diagnostics),
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
            "per_year_start_date_reuse_within_batch": False,
            "terminal_target_ownership": (
                "H1/H4 external pivot, or M15 equal-level liquidity pool"
            ),
            "margin_safety": asdict(_MARGIN_CONFIG),
            "margin_model_boundary": (
                "conservative ENGINEERING_V0 isolated-linear estimate; live must "
                "refresh symbol/notional maintenance brackets before order"
            ),
        }
    )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    base._args = _strict_args
    base.sample_trials = sample_trials_without_year_reuse
    base._build_authority_sets = _build_authority_sets
    base._assemble_candidate = _assemble_candidate
    status = int(base.main())
    _annotate_summary()
    return status


if __name__ == "__main__":
    raise SystemExit(main())

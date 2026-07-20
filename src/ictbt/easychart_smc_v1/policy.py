from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Iterable

import pandas as pd


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class Narrative(str, Enum):
    REVERSAL = "reversal"
    CONTINUATION = "continuation"


class ExecutionModel(str, Enum):
    SWEEP_MSS_RETURN = "sweep_mss_return"
    BREAK_ACCEPT_RETEST = "break_accept_retest"
    DELIVERED_OB_REENTRY = "delivered_ob_reentry"


class EventKind(str, Enum):
    SWEEP_RECLAIM = "sweep_reclaim"
    BREAK_ACCEPTANCE = "break_acceptance"


class EntryArray(str, Enum):
    ORDER_BLOCK = "order_block"
    FAIR_VALUE_GAP = "fair_value_gap"
    OB_FVG_OVERLAP = "ob_fvg_overlap"
    BROKEN_BOUNDARY = "broken_boundary"


class TargetKind(str, Enum):
    EXTERNAL_LIQUIDITY = "external_liquidity"
    CONFIRMED_PIVOT = "confirmed_pivot"
    OPPOSING_ORDER_BLOCK = "opposing_order_block"
    FAIR_VALUE_GAP = "fair_value_gap"


class ManagementPlan(str, Enum):
    FULL_AT_FIRST_OBSTACLE = "full_at_first_obstacle"
    HALF_AT_ONE_R_RUNNER_TO_FIRST_OBSTACLE = (
        "half_at_one_r_runner_to_first_obstacle"
    )


class AuthorityStatus(str, Enum):
    OBSERVING = "observing"
    ARMED = "armed"
    ENTRY_PENDING = "entry_pending"
    OPEN = "open"
    DEPARTURE_REQUIRED = "departure_required"
    REARMED = "rearmed"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


def _utc(value: object, *, name: str) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tz is None:
        raise ValueError(f"{name} must be timezone-aware")
    return result.tz_convert("UTC")


def _positive(value: float, *, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


def _non_negative(value: float, *, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return result


@dataclass(frozen=True, slots=True)
class PriceZone:
    low: float
    high: float

    def __post_init__(self) -> None:
        low = _positive(self.low, name="zone.low")
        high = _positive(self.high, name="zone.high")
        if high < low:
            raise ValueError("zone.high cannot be below zone.low")
        object.__setattr__(self, "low", low)
        object.__setattr__(self, "high", high)

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0

    def contains(self, price: float) -> bool:
        value = _positive(price, name="price")
        return self.low <= value <= self.high


@dataclass(frozen=True, slots=True)
class LiquidityObjective:
    objective_id: str
    symbol: str
    side: Side
    kind: TargetKind
    zone: PriceZone
    known_at: pd.Timestamp
    external: bool
    consumed: bool = False
    paired_liquidity_id: str | None = None

    def __post_init__(self) -> None:
        if not self.objective_id or not self.symbol:
            raise ValueError("objective identity fields are required")
        object.__setattr__(self, "known_at", _utc(self.known_at, name="known_at"))

    @property
    def terminal_eligible(self) -> bool:
        # An imbalance can be a reaction area, but it is not a terminal objective
        # unless a separately identified liquidity level owns the same area.
        if self.kind is TargetKind.FAIR_VALUE_GAP:
            return self.paired_liquidity_id is not None
        return True

    def order_price(self) -> float:
        return self.zone.low if self.side is Side.LONG else self.zone.high


@dataclass(frozen=True, slots=True)
class HigherTimeframeContext:
    context_id: str
    symbol: str
    side: Side
    known_at: pd.Timestamp
    draw_on_liquidity: LiquidityObjective
    location_valid: bool
    h1_aligned: bool
    h4_direct_conflict: bool
    at_external_liquidity: bool

    def __post_init__(self) -> None:
        if not self.context_id or not self.symbol:
            raise ValueError("context identity fields are required")
        known = _utc(self.known_at, name="known_at")
        if self.draw_on_liquidity.symbol != self.symbol:
            raise ValueError("context and objective symbols must match")
        if self.draw_on_liquidity.side is not self.side:
            raise ValueError("context and objective sides must match")
        if self.draw_on_liquidity.known_at > known:
            raise ValueError("draw on liquidity must be known by context time")
        object.__setattr__(self, "known_at", known)


@dataclass(frozen=True, slots=True)
class LiquidityEventEvidence:
    event_id: str
    symbol: str
    side: Side
    kind: EventKind
    known_at: pd.Timestamp
    level: float
    extreme: float
    external_liquidity: bool
    reclaimed: bool = False
    boundary_broken_by_close: bool = False
    accepted_after_break: bool = False
    opposing_sweep_after_event: bool = False

    def __post_init__(self) -> None:
        if not self.event_id or not self.symbol:
            raise ValueError("event identity fields are required")
        object.__setattr__(self, "known_at", _utc(self.known_at, name="known_at"))
        object.__setattr__(self, "level", _positive(self.level, name="level"))
        object.__setattr__(self, "extreme", _positive(self.extreme, name="extreme"))


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    delivery_id: str
    symbol: str
    side: Side
    known_at: pd.Timestamp
    source_event_id: str
    entry_array: EntryArray
    zone: PriceZone
    displacement_body_atr: float
    broke_preexisting_swing: bool
    fresh: bool
    return_number: int
    invalidation_extreme: float
    micro_rearm_confirmed: bool = False

    def __post_init__(self) -> None:
        if not self.delivery_id or not self.symbol or not self.source_event_id:
            raise ValueError("delivery identity fields are required")
        object.__setattr__(self, "known_at", _utc(self.known_at, name="known_at"))
        object.__setattr__(
            self,
            "displacement_body_atr",
            _non_negative(self.displacement_body_atr, name="displacement_body_atr"),
        )
        if self.return_number < 1:
            raise ValueError("return_number must be at least one")
        object.__setattr__(
            self,
            "invalidation_extreme",
            _positive(self.invalidation_extreme, name="invalidation_extreme"),
        )


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    intent_id: str
    symbol: str
    side: Side
    model: ExecutionModel
    narrative: Narrative
    known_at: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float
    stop: float
    context: HigherTimeframeContext
    event: LiquidityEventEvidence
    delivery: DeliveryEvidence
    targets: tuple[LiquidityObjective, ...]
    episode: int = 0
    previous_position_closed: bool = True
    departure_r: float = 0.0
    authority_invalidated: bool = False

    def __post_init__(self) -> None:
        if not self.intent_id or not self.symbol:
            raise ValueError("intent identity fields are required")
        known = _utc(self.known_at, name="known_at")
        entry_time = _utc(self.entry_time, name="entry_time")
        if entry_time < known:
            raise ValueError("entry_time cannot precede known_at")
        entry = _positive(self.entry, name="entry")
        stop = _positive(self.stop, name="stop")
        if self.side is Side.LONG and stop >= entry:
            raise ValueError("long stop must be below entry")
        if self.side is Side.SHORT and stop <= entry:
            raise ValueError("short stop must be above entry")
        if self.episode < 0:
            raise ValueError("episode cannot be negative")
        departure = _non_negative(self.departure_r, name="departure_r")
        for component_symbol in (
            self.context.symbol,
            self.event.symbol,
            self.delivery.symbol,
        ):
            if component_symbol != self.symbol:
                raise ValueError("intent components must use the same symbol")
        for component_side in (
            self.context.side,
            self.event.side,
            self.delivery.side,
        ):
            if component_side is not self.side:
                raise ValueError("intent components must use the same side")
        if not self.delivery.zone.contains(entry):
            raise ValueError("entry must be inside the owned delivery zone")
        object.__setattr__(self, "known_at", known)
        object.__setattr__(self, "entry_time", entry_time)
        object.__setattr__(self, "entry", entry)
        object.__setattr__(self, "stop", stop)
        object.__setattr__(self, "departure_r", departure)


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    risk_fraction: float = 0.03
    minimum_displacement_body_atr: float = 0.5
    minimum_net_target_r: float = 0.35
    maximum_required_leverage: float = 10.0
    authority_lifetime: pd.Timedelta = pd.Timedelta(hours=72)
    minimum_rearm_departure_r: float = 1.0
    maximum_reentry_episode: int = 4
    partial_management_threshold_r: float = 1.4
    maker_entry_fee_rate: float = 0.0002
    maker_target_fee_rate: float = 0.0002
    taker_stop_fee_rate: float = 0.0006
    adverse_stop_slippage_rate: float = 0.0002

    def __post_init__(self) -> None:
        risk = float(self.risk_fraction)
        if not 0 < risk < 1:
            raise ValueError("risk_fraction must be between zero and one")
        if self.maximum_reentry_episode < 0:
            raise ValueError("maximum_reentry_episode cannot be negative")
        if self.authority_lifetime <= pd.Timedelta(0):
            raise ValueError("authority_lifetime must be positive")
        for name in (
            "minimum_displacement_body_atr",
            "minimum_net_target_r",
            "maximum_required_leverage",
            "minimum_rearm_departure_r",
            "partial_management_threshold_r",
            "maker_entry_fee_rate",
            "maker_target_fee_rate",
            "taker_stop_fee_rate",
            "adverse_stop_slippage_rate",
        ):
            _non_negative(getattr(self, name), name=name)


@dataclass(frozen=True, slots=True)
class RiskSizing:
    equity: float
    risk_budget: float
    quantity: float
    entry_notional: float
    required_leverage: float
    all_in_stop_loss_per_unit: float


@dataclass(frozen=True, slots=True)
class ApprovedTrade:
    intent: ExecutionIntent
    target: LiquidityObjective
    gross_target_r: float
    net_target_r: float
    management: ManagementPlan
    sizing: RiskSizing | None
    structural_rank: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    approved: bool
    reasons: tuple[str, ...]
    trade: ApprovedTrade | None = None


def _adverse_stop_fill(intent: ExecutionIntent, config: PolicyConfig) -> float:
    slip = config.adverse_stop_slippage_rate
    if intent.side is Side.LONG:
        return intent.stop * (1.0 - slip)
    return intent.stop * (1.0 + slip)


def _all_in_stop_loss_per_unit(
    intent: ExecutionIntent, config: PolicyConfig
) -> float:
    stop_fill = _adverse_stop_fill(intent, config)
    entry_fee = intent.entry * config.maker_entry_fee_rate
    stop_fee = stop_fill * config.taker_stop_fee_rate
    price_loss = (
        intent.entry - stop_fill
        if intent.side is Side.LONG
        else stop_fill - intent.entry
    )
    loss = price_loss + entry_fee + stop_fee
    if loss <= 0:
        raise ValueError("all-in stop loss must be positive")
    return loss


def _target_profit_per_unit(
    intent: ExecutionIntent,
    target_price: float,
    config: PolicyConfig,
) -> float:
    entry_fee = intent.entry * config.maker_entry_fee_rate
    target_fee = target_price * config.maker_target_fee_rate
    if intent.side is Side.LONG:
        return target_price - intent.entry - entry_fee - target_fee
    return intent.entry - target_price - entry_fee - target_fee


def size_exact_risk(
    intent: ExecutionIntent,
    *,
    equity: float,
    config: PolicyConfig,
) -> RiskSizing:
    account_equity = _positive(equity, name="equity")
    loss_per_unit = _all_in_stop_loss_per_unit(intent, config)
    risk_budget = account_equity * config.risk_fraction
    quantity = risk_budget / loss_per_unit
    entry_notional = quantity * intent.entry
    leverage = entry_notional / account_equity
    return RiskSizing(
        equity=account_equity,
        risk_budget=risk_budget,
        quantity=quantity,
        entry_notional=entry_notional,
        required_leverage=leverage,
        all_in_stop_loss_per_unit=loss_per_unit,
    )


def _target_metrics(
    intent: ExecutionIntent,
    target: LiquidityObjective,
    config: PolicyConfig,
) -> tuple[float, float]:
    price = target.order_price()
    gross_risk = abs(intent.entry - intent.stop)
    gross_reward = (
        price - intent.entry if intent.side is Side.LONG else intent.entry - price
    )
    gross_r = gross_reward / gross_risk
    net_profit = _target_profit_per_unit(intent, price, config)
    net_r = net_profit / _all_in_stop_loss_per_unit(intent, config)
    return gross_r, net_r


def _eligible_targets(
    intent: ExecutionIntent,
    config: PolicyConfig,
) -> list[tuple[LiquidityObjective, float, float]]:
    output: list[tuple[LiquidityObjective, float, float]] = []
    for target in intent.targets:
        if target.symbol != intent.symbol or target.side is not intent.side:
            continue
        if target.known_at > intent.known_at or target.consumed:
            continue
        if not target.terminal_eligible:
            continue
        price = target.order_price()
        in_direction = (
            price > intent.entry
            if intent.side is Side.LONG
            else price < intent.entry
        )
        if not in_direction:
            continue
        gross_r, net_r = _target_metrics(intent, target, config)
        output.append((target, gross_r, net_r))
    output.sort(
        key=lambda item: (
            abs(item[0].order_price() - intent.entry),
            item[0].known_at,
            item[0].objective_id,
        )
    )
    return output


def _causal_reasons(intent: ExecutionIntent, config: PolicyConfig) -> list[str]:
    reasons: list[str] = []
    context = intent.context
    event = intent.event
    delivery = intent.delivery

    if not (
        context.known_at <= event.known_at <= delivery.known_at <= intent.known_at
    ):
        reasons.append("non_causal_evidence_timeline")
    if event.event_id != delivery.source_event_id:
        reasons.append("delivery_does_not_own_event")
    if delivery.displacement_body_atr < config.minimum_displacement_body_atr:
        reasons.append("insufficient_displacement")
    if not delivery.broke_preexisting_swing:
        reasons.append("no_meaningful_structure_break")
    if not delivery.fresh:
        reasons.append("stale_execution_array")
    if intent.authority_invalidated:
        reasons.append("authority_invalidated")
    if intent.entry_time - context.known_at > config.authority_lifetime:
        reasons.append("authority_expired")
    if not context.location_valid:
        reasons.append("invalid_higher_timeframe_location")
    if context.draw_on_liquidity.consumed:
        reasons.append("draw_on_liquidity_already_consumed")

    if intent.narrative is Narrative.REVERSAL:
        if intent.model is not ExecutionModel.SWEEP_MSS_RETURN:
            reasons.append("reversal_requires_sweep_mss_return")
        if event.kind is not EventKind.SWEEP_RECLAIM:
            reasons.append("reversal_requires_sweep_reclaim")
        if not event.external_liquidity or not context.at_external_liquidity:
            reasons.append("reversal_not_at_external_liquidity")
        if not event.reclaimed:
            reasons.append("swept_liquidity_not_reclaimed")
        if delivery.return_number != 1:
            reasons.append("reversal_requires_first_clean_return")
        stop_invalid = (
            intent.stop >= event.extreme
            if intent.side is Side.LONG
            else intent.stop <= event.extreme
        )
        if stop_invalid:
            reasons.append("stop_not_beyond_sweep_invalidation")

    elif intent.narrative is Narrative.CONTINUATION:
        if intent.model is ExecutionModel.BREAK_ACCEPT_RETEST:
            if event.kind is not EventKind.BREAK_ACCEPTANCE:
                reasons.append("continuation_requires_break_acceptance")
            if not event.boundary_broken_by_close:
                reasons.append("boundary_not_broken_by_close")
            if not event.accepted_after_break:
                reasons.append("break_not_accepted")
            if delivery.return_number != 1:
                reasons.append("continuation_requires_first_retest")
        elif intent.model is ExecutionModel.DELIVERED_OB_REENTRY:
            if intent.episode < 1:
                reasons.append("reentry_requires_positive_episode")
            if intent.episode > config.maximum_reentry_episode:
                reasons.append("reentry_episode_limit_exceeded")
            if not intent.previous_position_closed:
                reasons.append("reentry_cannot_pyramid")
            if intent.departure_r < config.minimum_rearm_departure_r:
                reasons.append("insufficient_departure_for_rearm")
            if not delivery.micro_rearm_confirmed:
                reasons.append("reentry_lacks_fresh_micro_delivery")
        else:
            reasons.append("unknown_continuation_model")
        if not context.h1_aligned:
            reasons.append("continuation_not_h1_aligned")
        if context.h4_direct_conflict:
            reasons.append("continuation_h4_direct_conflict")
        if event.opposing_sweep_after_event:
            reasons.append("opposing_liquidity_event_supersedes_continuation")
        stop_invalid = (
            intent.stop >= delivery.invalidation_extreme
            if intent.side is Side.LONG
            else intent.stop <= delivery.invalidation_extreme
        )
        if stop_invalid:
            reasons.append("stop_not_beyond_delivery_invalidation")
    else:
        reasons.append("unknown_narrative")

    return reasons


def evaluate_intent(
    intent: ExecutionIntent,
    *,
    config: PolicyConfig = PolicyConfig(),
    equity: float | None = None,
) -> PolicyDecision:
    reasons = _causal_reasons(intent, config)
    targets = _eligible_targets(intent, config)
    if not targets:
        reasons.append("no_preexisting_structural_target")
    else:
        # The nearest valid obstacle owns the day-trade exit. Skipping it to claim
        # a farther R multiple would contradict the actual path price must traverse.
        nearest_target, gross_r, net_r = targets[0]
        if net_r < config.minimum_net_target_r:
            reasons.append("nearest_obstacle_does_not_pay_minimum_net_r")

    sizing: RiskSizing | None = None
    if equity is not None:
        sizing = size_exact_risk(intent, equity=equity, config=config)
        if sizing.required_leverage > config.maximum_required_leverage:
            reasons.append("exact_three_percent_risk_requires_unsafe_leverage")

    if reasons:
        return PolicyDecision(False, tuple(dict.fromkeys(reasons)), None)

    nearest_target, gross_r, net_r = targets[0]
    management = (
        ManagementPlan.HALF_AT_ONE_R_RUNNER_TO_FIRST_OBSTACLE
        if net_r >= config.partial_management_threshold_r
        else ManagementPlan.FULL_AT_FIRST_OBSTACLE
    )
    kind_rank = {
        TargetKind.EXTERNAL_LIQUIDITY: 4.0,
        TargetKind.CONFIRMED_PIVOT: 3.0,
        TargetKind.OPPOSING_ORDER_BLOCK: 2.0,
        TargetKind.FAIR_VALUE_GAP: 1.0,
    }[nearest_target.kind]
    structural_rank = (
        1.0 if nearest_target.external else 0.0,
        kind_rank,
        net_r,
        min(intent.delivery.displacement_body_atr, 5.0),
        -((sizing.required_leverage if sizing else 0.0)),
    )
    return PolicyDecision(
        True,
        (),
        ApprovedTrade(
            intent=intent,
            target=nearest_target,
            gross_target_r=gross_r,
            net_target_r=net_r,
            management=management,
            sizing=sizing,
            structural_rank=structural_rank,
        ),
    )


def choose_global_trade(
    intents: Iterable[ExecutionIntent],
    *,
    config: PolicyConfig = PolicyConfig(),
    equity: float | None = None,
    pending_or_open: bool = False,
) -> PolicyDecision:
    if pending_or_open:
        return PolicyDecision(False, ("global_slot_occupied",), None)

    intent_list = list(intents)
    if not intent_list:
        return PolicyDecision(False, ("no_candidate",), None)

    # Opposing narratives on the same symbol at the same decision timestamp are
    # ambiguity, not confluence. A deterministic family priority must not hide it.
    by_symbol_time: dict[tuple[str, pd.Timestamp], set[Side]] = {}
    for intent in intent_list:
        by_symbol_time.setdefault((intent.symbol, intent.known_at), set()).add(intent.side)
    ambiguous = {
        key for key, sides in by_symbol_time.items() if len(sides) > 1
    }

    approved: list[ApprovedTrade] = []
    rejection_reasons: list[str] = []
    for intent in intent_list:
        if (intent.symbol, intent.known_at) in ambiguous:
            rejection_reasons.append("opposing_same_symbol_narratives")
            continue
        decision = evaluate_intent(intent, config=config, equity=equity)
        if decision.approved and decision.trade is not None:
            approved.append(decision.trade)
        else:
            rejection_reasons.extend(decision.reasons)

    if not approved:
        reasons = tuple(dict.fromkeys(rejection_reasons or ["no_approved_candidate"]))
        return PolicyDecision(False, reasons, None)

    approved.sort(
        key=lambda trade: (
            trade.intent.entry_time,
            tuple(-value for value in trade.structural_rank),
            trade.intent.symbol,
            trade.intent.intent_id,
        )
    )
    return PolicyDecision(True, (), approved[0])


@dataclass(slots=True)
class AuthorityRuntime:
    authority_id: str
    status: AuthorityStatus = AuthorityStatus.OBSERVING
    episode: int = 0
    last_exit_time: pd.Timestamp | None = None
    last_exit_was_stop: bool = False
    maximum_departure_r: float = 0.0

    def arm(self) -> None:
        if self.status not in {AuthorityStatus.OBSERVING, AuthorityStatus.REARMED}:
            raise RuntimeError(f"cannot arm from {self.status.value}")
        self.status = AuthorityStatus.ARMED

    def submit_entry(self, *, global_slot_occupied: bool) -> None:
        if global_slot_occupied:
            raise RuntimeError("global BTC/ETH slot is occupied")
        if self.status not in {AuthorityStatus.ARMED, AuthorityStatus.REARMED}:
            raise RuntimeError(f"cannot submit entry from {self.status.value}")
        self.status = AuthorityStatus.ENTRY_PENDING

    def fill(self) -> None:
        if self.status is not AuthorityStatus.ENTRY_PENDING:
            raise RuntimeError("only a pending entry can fill")
        self.status = AuthorityStatus.OPEN

    def close(self, *, closed_at: object, stopped: bool) -> None:
        if self.status is not AuthorityStatus.OPEN:
            raise RuntimeError("only an open position can close")
        self.last_exit_time = _utc(closed_at, name="closed_at")
        self.last_exit_was_stop = bool(stopped)
        self.maximum_departure_r = 0.0
        if stopped:
            self.status = AuthorityStatus.INVALIDATED
        else:
            self.status = AuthorityStatus.DEPARTURE_REQUIRED
            self.episode += 1

    def observe_departure(
        self,
        *,
        departure_r: float,
        micro_delivery_confirmed: bool,
        config: PolicyConfig = PolicyConfig(),
    ) -> None:
        if self.status is not AuthorityStatus.DEPARTURE_REQUIRED:
            raise RuntimeError("departure is only observed after a profitable close")
        value = _non_negative(departure_r, name="departure_r")
        self.maximum_departure_r = max(self.maximum_departure_r, value)
        if (
            self.maximum_departure_r >= config.minimum_rearm_departure_r
            and micro_delivery_confirmed
            and self.episode <= config.maximum_reentry_episode
        ):
            self.status = AuthorityStatus.REARMED

    def invalidate(self) -> None:
        self.status = AuthorityStatus.INVALIDATED

    def expire(self) -> None:
        if self.status not in {AuthorityStatus.INVALIDATED, AuthorityStatus.OPEN}:
            self.status = AuthorityStatus.EXPIRED

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .domain import (
    FormationBar,
    OrderBlock,
    OwnedM15OverlapAuthority,
    OwnedM15StopOwner,
    PriceZone,
    Side,
    StrictPivot,
    TargetCandidate,
    Timeframe,
)
from .features import (
    TIMEFRAME_DELTA,
    intersect_zones,
    pivot_is_consumed,
    zone_is_consumed,
)
from .pipeline import FeatureBook, _frame_as_of, _order_block_is_active
from .strategy import select_initial_target


@dataclass(frozen=True, slots=True)
class V06BuildDiagnostics:
    m15_anchor_candidates: int
    displacement_roots: int
    duplicate_anchor_variants: int
    owned_break_roots: int
    roots_without_protected_pivot: int
    overlap_pairs: int
    inactive_partner_pairs: int
    freshness_rejections: int
    roots_without_orderable_partner: int
    targets_missing_at_anchor: int
    departures_missing: int
    target_used_before_departure: int
    formation_stop_used_before_departure: int
    protected_stop_used_before_departure: int
    at_anchor_close_scenes: int
    later_fresh_scenes: int
    h1_m15_scenes: int
    m15_m5_scenes: int
    authorities: int


@dataclass(frozen=True, slots=True)
class V06BuildResult:
    authorities: tuple[OwnedM15OverlapAuthority, ...]
    diagnostics: V06BuildDiagnostics


@dataclass(frozen=True, slots=True)
class _PairCandidate:
    anchor: OrderBlock
    partner: OrderBlock
    pair_type: str
    partner_timing: str
    zone: PriceZone
    pair_known_at: pd.Timestamp
    break_pivot: StrictPivot
    protected_pivot: StrictPivot
    formation_stop_extreme: float
    formation_initial_stop: float
    protected_stop_extreme: float
    protected_initial_stop: float
    destination: TargetCandidate


def _bar(
    opened: pd.Timestamp,
    row: pd.Series,
    timeframe: Timeframe,
) -> FormationBar:
    return FormationBar(
        open_time=opened,
        close_time=opened + TIMEFRAME_DELTA[timeframe],
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def _latest_pivot(
    book: FeatureBook,
    *,
    kind: str,
    known_by: pd.Timestamp,
) -> StrictPivot | None:
    eligible = [
        pivot
        for pivot in book.pivots[Timeframe.M15]
        if pivot.kind == kind and pivot.known_at <= known_by
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda item: (item.pivot_time, item.known_at, item.pivot_id),
    )


def _owned_m15_break_pivot(
    book: FeatureBook,
    anchor: OrderBlock,
) -> StrictPivot | None:
    """Return the latest already-known M15 pivot directly broken by anchor D."""

    if anchor.timeframe is not Timeframe.M15:
        return None
    displacement = anchor.formation_bars[-1]
    expected_direction = (
        displacement.bullish if anchor.side is Side.LONG else displacement.bearish
    )
    if not expected_direction:
        return None
    pivot = _latest_pivot(
        book,
        kind="high" if anchor.side is Side.LONG else "low",
        known_by=displacement.open_time,
    )
    if pivot is None:
        return None
    frame = book.frames[Timeframe.M15]
    index = int(frame.index.searchsorted(displacement.open_time, side="left"))
    if (
        index <= 0
        or index >= len(frame)
        or frame.index[index] != displacement.open_time
    ):
        return None
    previous_close = float(frame.iloc[index - 1]["close"])
    if anchor.side is Side.LONG:
        owns_break = (
            previous_close <= pivot.price + 1e-12
            and displacement.close >= pivot.price + book.tick_size - 1e-12
        )
    else:
        owns_break = (
            previous_close >= pivot.price - 1e-12
            and displacement.close <= pivot.price - book.tick_size + 1e-12
        )
    return pivot if owns_break else None


def _protected_m15_pivot(
    book: FeatureBook,
    anchor: OrderBlock,
) -> StrictPivot | None:
    displacement = anchor.formation_bars[-1]
    return _latest_pivot(
        book,
        kind="low" if anchor.side is Side.LONG else "high",
        known_by=displacement.open_time,
    )


def _target_candidates_at_anchor(
    book: FeatureBook,
    anchor: OrderBlock,
) -> tuple[TargetCandidate, ...]:
    """Build only independent structures already present at anchor birth."""

    side = anchor.side
    cutoff = anchor.known_at
    output: list[TargetCandidate] = []
    pivot_kind = "high" if side is Side.LONG else "low"
    for timeframe in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
        frame = _frame_as_of(book, timeframe, cutoff)
        for pivot in book.pivots[timeframe]:
            if (
                pivot.kind != pivot_kind
                or pivot.known_at > cutoff
                or pivot_is_consumed(
                    pivot,
                    frame,
                    tick_size=book.tick_size,
                )
            ):
                continue
            output.append(
                TargetCandidate(
                    candidate_id=f"v06:{timeframe.value}:pivot:{pivot.pivot_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="pivot",
                    zone=PriceZone(pivot.price, pivot.price),
                    known_at=pivot.known_at,
                    source_id=pivot.pivot_id,
                )
            )
        for block in book.order_blocks[timeframe]:
            if (
                block.side is side
                or block.known_at > cutoff
                or zone_is_consumed(
                    block.zone,
                    frame,
                    travel_side=side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=block.known_at,
                )
            ):
                continue
            output.append(
                TargetCandidate(
                    candidate_id=f"v06:{timeframe.value}:ob:{block.ob_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="order_block",
                    zone=block.zone,
                    known_at=block.known_at,
                    source_id=block.ob_id,
                )
            )
        for gap in book.fvgs[timeframe]:
            if (
                gap.side is side
                or gap.known_at > cutoff
                or zone_is_consumed(
                    gap.zone,
                    frame,
                    travel_side=side,
                    timeframe=timeframe,
                    tick_size=book.tick_size,
                    after=gap.known_at,
                )
            ):
                continue
            output.append(
                TargetCandidate(
                    candidate_id=f"v06:{timeframe.value}:fvg:{gap.fvg_id}",
                    symbol=book.symbol,
                    trade_side=side,
                    kind="fvg",
                    zone=gap.zone,
                    known_at=gap.known_at,
                    source_id=gap.fvg_id,
                )
            )
    return tuple(output)


def _destination_for_entry(
    book: FeatureBook,
    anchor: OrderBlock,
    *,
    entry: float,
    candidates: tuple[TargetCandidate, ...] | None = None,
) -> TargetCandidate | None:
    selection = select_initial_target(
        (
            _target_candidates_at_anchor(book, anchor)
            if candidates is None
            else candidates
        ),
        side=anchor.side,
        entry_price=entry,
        tick_size=book.tick_size,
    )
    return selection.target


def _anchor_is_fresh_before_partner_formation(
    book: FeatureBook,
    anchor: OrderBlock,
    partner: OrderBlock,
) -> bool:
    """Treat the partner's own formation bars as construction, not a revisit."""

    if partner.known_at <= anchor.known_at:
        return True
    formation_started = min(bar.open_time for bar in partner.formation_bars)
    if formation_started <= anchor.known_at:
        return True
    frame = book.frames[Timeframe.M5]
    bars = frame.loc[
        (frame.index >= anchor.known_at) & (frame.index < formation_started)
    ]
    touched = (
        (bars["low"] <= anchor.zone.high)
        & (bars["high"] >= anchor.zone.low)
    )
    return not bool(touched.any())


def _first_departure_bar(
    book: FeatureBook,
    *,
    side: Side,
    zone: PriceZone,
    pair_known_at: pd.Timestamp,
) -> FormationBar | None:
    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    eligible = frame.loc[closes >= pair_known_at]
    departed = (
        eligible["close"] >= zone.high + book.tick_size - 1e-12
        if side is Side.LONG
        else eligible["close"] <= zone.low - book.tick_size + 1e-12
    )
    hits = eligible.index[departed.to_numpy()]
    if len(hits) == 0:
        return None
    opened = hits[0]
    return _bar(opened, frame.loc[opened], Timeframe.M5)


def _level_touched(
    book: FeatureBook,
    *,
    side: Side,
    price: float,
    favorable: bool,
    after: pd.Timestamp,
    through: pd.Timestamp,
) -> bool:
    frame = book.frames[Timeframe.M5]
    closes = frame.index + TIMEFRAME_DELTA[Timeframe.M5]
    bars = frame.loc[(closes > after) & (closes <= through)]
    if bars.empty:
        return False
    if favorable:
        return bool(
            (bars["high"] >= price).any()
            if side is Side.LONG
            else (bars["low"] <= price).any()
        )
    return bool(
        (bars["low"] <= price).any()
        if side is Side.LONG
        else (bars["high"] >= price).any()
    )


def _stop_levels(
    book: FeatureBook,
    anchor: OrderBlock,
    protected: StrictPivot,
) -> tuple[float, float, float, float] | None:
    formation_extreme = (
        min(bar.low for bar in anchor.formation_bars)
        if anchor.side is Side.LONG
        else max(bar.high for bar in anchor.formation_bars)
    )
    protected_extreme = protected.price
    formation_stop = (
        formation_extreme - book.tick_size
        if anchor.side is Side.LONG
        else formation_extreme + book.tick_size
    )
    protected_stop = (
        protected_extreme - book.tick_size
        if anchor.side is Side.LONG
        else protected_extreme + book.tick_size
    )
    if formation_stop <= 0 or protected_stop <= 0:
        return None
    return formation_extreme, formation_stop, protected_extreme, protected_stop


def _pair_sort_key(candidate: _PairCandidate) -> tuple[object, ...]:
    return (
        candidate.pair_known_at,
        0 if candidate.pair_type == "m15_m5" else 1,
        candidate.zone.width,
        -candidate.partner.known_at.value,
        candidate.anchor.ob_id,
        candidate.partner.ob_id,
    )


def build_owned_m15_overlap_result(
    book: FeatureBook,
    *,
    stop_owner: OwnedM15StopOwner,
) -> V06BuildResult:
    if stop_owner not in {"m15_anchor_formation", "protected_m15_swing"}:
        raise ValueError("unknown V0.6 stop owner")

    anchors = book.order_blocks[Timeframe.M15]
    grouped: dict[tuple[str, Side, pd.Timestamp], list[OrderBlock]] = {}
    for anchor in anchors:
        displacement = anchor.formation_bars[-1]
        grouped.setdefault(
            (anchor.symbol, anchor.side, displacement.open_time),
            [],
        ).append(anchor)

    owned_break_roots = 0
    roots_without_protected = 0
    overlap_pairs = 0
    inactive_pairs = 0
    freshness_rejections = 0
    roots_without_partner = 0
    targets_missing = 0
    departures_missing = 0
    target_used = 0
    formation_stop_used = 0
    protected_stop_used = 0
    output: list[OwnedM15OverlapAuthority] = []
    target_universe_cache: dict[str, tuple[TargetCandidate, ...]] = {}
    destination_cache: dict[tuple[str, float], TargetCandidate | None] = {}

    partners = (
        *book.order_blocks[Timeframe.H1],
        *book.order_blocks[Timeframe.M5],
    )
    for (symbol, side, displacement_open), variants in sorted(
        grouped.items(),
        key=lambda item: (item[0][2], item[0][1].value, item[0][0]),
    ):
        root_candidates: list[_PairCandidate] = []
        root_has_owned_break = False
        root_has_protected = False
        for anchor in variants:
            break_pivot = _owned_m15_break_pivot(book, anchor)
            if break_pivot is None:
                continue
            root_has_owned_break = True
            protected = _protected_m15_pivot(book, anchor)
            if protected is None:
                continue
            root_has_protected = True
            stops = _stop_levels(book, anchor, protected)
            if stops is None:
                continue
            (
                formation_extreme,
                formation_stop,
                protected_extreme,
                protected_stop,
            ) = stops
            for partner in partners:
                if partner.side is not side:
                    continue
                zone = intersect_zones(
                    (anchor.zone, partner.zone),
                    minimum_width=book.tick_size,
                )
                if zone is None:
                    continue
                overlap_pairs += 1
                pair_known_at = max(anchor.known_at, partner.known_at)
                if not _order_block_is_active(book, partner, as_of=pair_known_at):
                    inactive_pairs += 1
                    continue
                if not _anchor_is_fresh_before_partner_formation(
                    book,
                    anchor,
                    partner,
                ):
                    freshness_rejections += 1
                    continue
                entry = zone.high if side is Side.LONG else zone.low
                if (side is Side.LONG and not (
                    formation_stop < entry and protected_stop < entry
                )) or (side is Side.SHORT and not (
                    formation_stop > entry and protected_stop > entry
                )):
                    continue
                target_universe = target_universe_cache.get(anchor.ob_id)
                if target_universe is None:
                    target_universe = _target_candidates_at_anchor(book, anchor)
                    target_universe_cache[anchor.ob_id] = target_universe
                destination_key = (anchor.ob_id, entry)
                if destination_key not in destination_cache:
                    destination_cache[destination_key] = _destination_for_entry(
                        book,
                        anchor,
                        entry=entry,
                        candidates=target_universe,
                    )
                destination = destination_cache[destination_key]
                if destination is None:
                    targets_missing += 1
                    continue
                root_candidates.append(
                    _PairCandidate(
                        anchor=anchor,
                        partner=partner,
                        pair_type=(
                            "m15_m5"
                            if partner.timeframe is Timeframe.M5
                            else "h1_m15"
                        ),
                        partner_timing=(
                            "at_anchor_close"
                            if partner.known_at <= anchor.known_at
                            else "later_fresh"
                        ),
                        zone=zone,
                        pair_known_at=pair_known_at,
                        break_pivot=break_pivot,
                        protected_pivot=protected,
                        formation_stop_extreme=formation_extreme,
                        formation_initial_stop=formation_stop,
                        protected_stop_extreme=protected_extreme,
                        protected_initial_stop=protected_stop,
                        destination=destination,
                    )
                )
        if root_has_owned_break:
            owned_break_roots += 1
        if root_has_owned_break and not root_has_protected:
            roots_without_protected += 1
        if not root_candidates:
            if root_has_owned_break and root_has_protected:
                roots_without_partner += 1
            continue

        selected = min(root_candidates, key=_pair_sort_key)
        departure = _first_departure_bar(
            book,
            side=side,
            zone=selected.zone,
            pair_known_at=selected.pair_known_at,
        )
        if departure is None:
            departures_missing += 1
            continue
        through = departure.close_time
        if _level_touched(
            book,
            side=side,
            price=selected.destination.order_price,
            favorable=True,
            after=selected.anchor.known_at,
            through=through,
        ):
            target_used += 1
            continue
        if _level_touched(
            book,
            side=side,
            price=selected.formation_initial_stop,
            favorable=False,
            after=selected.anchor.known_at,
            through=through,
        ):
            formation_stop_used += 1
            continue
        if _level_touched(
            book,
            side=side,
            price=selected.protected_initial_stop,
            favorable=False,
            after=selected.anchor.known_at,
            through=through,
        ):
            protected_stop_used += 1
            continue

        root_id = (
            f"{symbol}:v06-owned-m15:{side.value}:"
            f"{displacement_open.isoformat()}"
        )
        chosen_extreme = (
            selected.formation_stop_extreme
            if stop_owner == "m15_anchor_formation"
            else selected.protected_stop_extreme
        )
        chosen_stop = (
            selected.formation_initial_stop
            if stop_owner == "m15_anchor_formation"
            else selected.protected_initial_stop
        )
        displacement = selected.anchor.formation_bars[-1]
        output.append(
            OwnedM15OverlapAuthority(
                authority_id=root_id,
                scene_root_id=root_id,
                symbol=symbol,
                side=side,
                anchor_ob=selected.anchor,
                partner_ob=selected.partner,
                pair_type=selected.pair_type,  # type: ignore[arg-type]
                partner_timing=selected.partner_timing,  # type: ignore[arg-type]
                break_pivot=selected.break_pivot,
                protected_pivot=selected.protected_pivot,
                break_bar=displacement,
                zone=selected.zone,
                pair_known_at=selected.pair_known_at,
                departure_bar=departure,
                known_at=departure.close_time,
                stop_owner=stop_owner,
                stop_extreme=chosen_extreme,
                initial_stop=chosen_stop,
                impulse_extreme=(
                    displacement.high
                    if side is Side.LONG
                    else displacement.low
                ),
                destination=selected.destination,
            )
        )

    authorities = tuple(
        sorted(output, key=lambda item: (item.known_at, item.authority_id))
    )
    diagnostics = V06BuildDiagnostics(
        m15_anchor_candidates=len(anchors),
        displacement_roots=len(grouped),
        duplicate_anchor_variants=len(anchors) - len(grouped),
        owned_break_roots=owned_break_roots,
        roots_without_protected_pivot=roots_without_protected,
        overlap_pairs=overlap_pairs,
        inactive_partner_pairs=inactive_pairs,
        freshness_rejections=freshness_rejections,
        roots_without_orderable_partner=roots_without_partner,
        targets_missing_at_anchor=targets_missing,
        departures_missing=departures_missing,
        target_used_before_departure=target_used,
        formation_stop_used_before_departure=formation_stop_used,
        protected_stop_used_before_departure=protected_stop_used,
        at_anchor_close_scenes=sum(
            item.partner_timing == "at_anchor_close" for item in authorities
        ),
        later_fresh_scenes=sum(
            item.partner_timing == "later_fresh" for item in authorities
        ),
        h1_m15_scenes=sum(item.pair_type == "h1_m15" for item in authorities),
        m15_m5_scenes=sum(item.pair_type == "m15_m5" for item in authorities),
        authorities=len(authorities),
    )
    return V06BuildResult(authorities, diagnostics)


def build_owned_m15_overlap_authorities(
    book: FeatureBook,
    *,
    stop_owner: OwnedM15StopOwner,
) -> tuple[OwnedM15OverlapAuthority, ...]:
    return build_owned_m15_overlap_result(
        book,
        stop_owner=stop_owner,
    ).authorities


__all__ = [
    "V06BuildDiagnostics",
    "V06BuildResult",
    "build_owned_m15_overlap_authorities",
    "build_owned_m15_overlap_result",
]

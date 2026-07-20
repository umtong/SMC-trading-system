from __future__ import annotations

from .domain import (
    B1Subtype,
    LiquidityEvent,
    PriceZone,
    Side,
    TargetCandidate,
    Timeframe,
)
from .pipeline import FeatureBook
from .v07 import (
    SrFlipFvgAuthority,
    _accepted,
    _active_boundaries,
    _boundary_is_in_fvg,
    _directional_break,
)


def _placeholder_target(
    *,
    book: FeatureBook,
    side: Side,
    source_id: str,
    price: float,
    known_at,
) -> TargetCandidate:
    """Create an internal construction marker replaced before order admission."""

    return TargetCandidate(
        candidate_id=f"v08-boundary-placeholder:{source_id}",
        symbol=book.symbol,
        trade_side=side,
        kind="impulse",
        zone=PriceZone(price, price),
        known_at=known_at,
        source_id=f"{source_id}:placeholder",
    )


def build_v08_boundary_candidates(
    book: FeatureBook,
) -> tuple[SrFlipFvgAuthority, ...]:
    """Return every H1/M15 boundary that independently owns V0.7 geometry.

    V0.7 intentionally chooses M15 precision before H1. That preference must not
    run before V0.8 evaluates higher-timeframe context, otherwise a valid H1
    range-expansion scene can disappear because an M15 line is nested inside the
    same FVG. This builder preserves all linked boundaries; V0.8 qualifies first
    and resolves one authority per scene afterward.
    """

    raw: list[SrFlipFvgAuthority] = []
    for gap in book.fvgs[Timeframe.M15]:
        a_bar, break_bar, acceptance_bar = gap.formation_bars
        linked = tuple(
            boundary
            for boundary in _active_boundaries(
                book,
                side=gap.side,
                break_bar=break_bar,
            )
            if _directional_break(
                side=gap.side,
                a_bar=a_bar,
                break_bar=break_bar,
                boundary=boundary.price,
                tick_size=book.tick_size,
            )
            and _accepted(
                side=gap.side,
                acceptance_bar=acceptance_bar,
                boundary=boundary.price,
                tick_size=book.tick_size,
            )
            and _boundary_is_in_fvg(gap, boundary=boundary.price)
        )
        if not linked:
            continue

        root_id = (
            f"v08-sr-flip-fvg-candidates:{book.symbol}:{gap.side.value}:"
            f"{break_bar.open_time.isoformat()}"
        )
        stop_extreme = (
            min(bar.low for bar in gap.formation_bars)
            if gap.side is Side.LONG
            else max(bar.high for bar in gap.formation_bars)
        )
        initial_stop = (
            stop_extreme - book.tick_size
            if gap.side is Side.LONG
            else stop_extreme + book.tick_size
        )
        impulse_extreme = (
            max(bar.high for bar in gap.formation_bars)
            if gap.side is Side.LONG
            else min(bar.low for bar in gap.formation_bars)
        )

        for boundary in linked:
            event = LiquidityEvent(
                event_id=(
                    f"{root_id}:boundary={boundary.pivot_id}:acceptance"
                ),
                symbol=book.symbol,
                timeframe=Timeframe.M15,
                subtype=B1Subtype.BREAK_RETEST,
                side=gap.side,
                node_id=boundary.pivot_id,
                node_price=boundary.price,
                event_time=break_bar.open_time,
                known_at=acceptance_bar.close_time,
                event_extreme=stop_extreme,
            )
            raw.append(
                SrFlipFvgAuthority(
                    authority_id=(
                        f"{root_id}|boundary={boundary.pivot_id}|fvg={gap.fvg_id}"
                    ),
                    scene_root_id=root_id,
                    symbol=book.symbol,
                    side=gap.side,
                    boundary_pivot=boundary,
                    liquidity_event=event,
                    fvg=gap,
                    break_bar=break_bar,
                    acceptance_bar=acceptance_bar,
                    zone=PriceZone(boundary.price, boundary.price),
                    known_at=gap.known_at,
                    stop_extreme=stop_extreme,
                    initial_stop=initial_stop,
                    impulse_extreme=impulse_extreme,
                    destination=_placeholder_target(
                        book=book,
                        side=gap.side,
                        source_id=gap.fvg_id,
                        price=impulse_extreme,
                        known_at=gap.known_at,
                    ),
                )
            )

    return tuple(
        sorted(
            raw,
            key=lambda authority: (
                authority.known_at,
                authority.scene_root_id,
                0
                if authority.boundary_pivot.timeframe is Timeframe.H1
                else 1,
                authority.authority_id,
            ),
        )
    )


__all__ = ["build_v08_boundary_candidates"]

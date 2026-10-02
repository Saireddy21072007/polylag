"""Entry and exit rules -- the part you will actually edit.

Every gate is a separate, named check that returns a reason string when it
fails. Nothing is silently skipped, and every rejection is journalled, so after
a week of paper trading you can count exactly which gate is killing your fills
and decide whether the edge was ever there.

Entry, in order (cheapest and most disqualifying checks first):

  1. market is live, accepting orders, and not about to resolve
  2. we have a fresh, sane, two-sided book
  3. we have a pre-news reference mid (no "before" price -> no measurable lag)
  4. the market has NOT already repriced (this is the actual lag test)
  5. spread and depth are tradable
  6. edge, after the cost buffer, clears the threshold
  7. the price is inside sane bounds

Exit, checked every cycle, first match wins:

  kill switch > time-to-resolution > stop loss > take profit > time stop
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ..config import StrategyConfig
from ..models import MarketRef, NewsEvent, OrderBook, Position, Signal
from ..news.matcher import TriggerMatch
from . import fair_value as fv

log = logging.getLogger("rules")


@dataclass(frozen=True)
class Evaluation:
    """The outcome of considering one (headline, market) pair."""

    accepted: bool
    reason: str
    gate: str
    signal: Optional[Signal] = None

    @staticmethod
    def reject(gate: str, reason: str) -> "Evaluation":
        return Evaluation(False, reason, gate)


def evaluate_entry(
    match: TriggerMatch,
    event: NewsEvent,
    market: MarketRef,
    book: OrderBook,
    pre_news_mid: Optional[float],
    cfg: StrategyConfig,
    max_book_staleness_ms: int,
    max_spread: float,
    min_depth_shares: float,
) -> Evaluation:
    """Decide whether this headline justifies buying `match.trigger.outcome`."""
    trigger = match.trigger
    outcome = trigger.outcome
    token_id = market.token_for(outcome)  # type: ignore[arg-type]

    # 1. market liveness ---------------------------------------------------- #
    if market.closed:
        return Evaluation.reject("market_live", "market is closed")
    if not market.accepting_orders:
        return Evaluation.reject("market_live", "market is not accepting orders")
    ttr = market.seconds_to_resolution()
    if ttr is not None and ttr < cfg.min_seconds_to_resolution:
        return Evaluation.reject(
            "time_to_resolution",
            f"only {ttr:.0f}s to resolution (min {cfg.min_seconds_to_resolution:.0f}s)",
        )

    # 2. book quality ------------------------------------------------------- #
    if book.token_id != token_id:
        return Evaluation.reject("book_identity", "book does not match target token")
    if book.age_ms() > max_book_staleness_ms:
        return Evaluation.reject(
            "book_fresh", f"book is {book.age_ms()}ms old (max {max_book_staleness_ms}ms)"
        )
    if book.best_bid is None or book.best_ask is None:
        return Evaluation.reject("book_two_sided", "book is one-sided or empty")
    if book.is_crossed():
        return Evaluation.reject("book_sane", "book is crossed -- bad data")

    ask = book.best_ask
    current_mid = book.mid
    assert current_mid is not None  # guaranteed by the two-sided check above

    # 3. pre-news anchor ---------------------------------------------------- #
    if pre_news_mid is None:
        return Evaluation.reject(
            "pre_news_anchor",
            "no mid observed before the headline -- cannot measure a lag",
        )

    view = fv.build_view(trigger, pre_news_mid, current_mid)

    # 4. the lag test ------------------------------------------------------- #
    # If the market has already moved in the direction we expect, someone was
    # faster. Chasing is how this strategy turns into buying tops.
    expected_direction = 1.0 if view.fair_value >= pre_news_mid else -1.0
    move_with_us = view.drift_since_news * expected_direction
    if move_with_us > cfg.max_price_move_since_news:
        return Evaluation.reject(
            "already_repriced",
            f"market already moved {move_with_us:+.3f} toward target "
            f"(max {cfg.max_price_move_since_news:.3f}) -- lag is gone",
        )

    # 5. microstructure ----------------------------------------------------- #
    spread = book.spread or 1.0
    if spread > max_spread:
        return Evaluation.reject(
            "spread", f"spread {spread:.3f} > max {max_spread:.3f}"
        )
    top_depth = book.asks[0].size if book.asks else 0.0
    if top_depth < min_depth_shares:
        return Evaluation.reject(
            "depth", f"top ask depth {top_depth:.0f} < min {min_depth_shares:.0f} shares"
        )

    # 6. edge --------------------------------------------------------------- #
    edge = fv.edge_for_buy(view.fair_value, ask, cfg.cost_buffer)
    if edge is None:
        return Evaluation.reject("edge", "no ask price available")
    if edge < cfg.min_edge:
        return Evaluation.reject(
            "edge",
            f"edge {edge:+.3f} after {cfg.cost_buffer:.3f} cost buffer "
            f"< min {cfg.min_edge:.3f}",
        )

    # 7. price sanity ------------------------------------------------------- #
    if ask < cfg.min_entry_price:
        return Evaluation.reject(
            "price_bounds", f"ask {ask:.3f} below min entry {cfg.min_entry_price:.3f}"
        )
    if ask > cfg.max_entry_price:
        return Evaluation.reject(
            "price_bounds", f"ask {ask:.3f} above max entry {cfg.max_entry_price:.3f}"
        )

    rationale = (
        f"{match.explanation}; pre-news mid {pre_news_mid:.3f}, now {current_mid:.3f}, "
        f"fair {view.fair_value:.3f}, ask {ask:.3f}, edge {edge:+.3f} "
        f"(headline seen {event.age_sec():.1f}s ago)"
    )
    signal = Signal(
        market=market,
        token_id=token_id,
        outcome=outcome,  # type: ignore[arg-type]
        side="BUY",
        reference_price=ask,
        fair_value=view.fair_value,
        edge=edge,
        trigger_name=trigger.name,
        news_event_id=event.event_id,
        news_title=event.title,
        rationale=rationale,
    )
    return Evaluation(True, rationale, "accepted", signal)


# --------------------------------------------------------------------------- #
# Exits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str
    tag: str  # take_profit | stop_loss | time_stop | resolution_guard | flatten


NO_EXIT = ExitDecision(False, "", "")


def evaluate_exit(
    position: Position,
    book: OrderBook,
    cfg: StrategyConfig,
    force: bool = False,
) -> ExitDecision:
    """First matching rule wins. Order encodes priority.

    We mark against the BID, not the mid: the bid is what we could actually
    receive right now. Marking at mid flatters every open position and is the
    most common way a paper P&L curve lies to you.
    """
    if force:
        return ExitDecision(True, "forced flatten (kill switch or shutdown)", "flatten")

    ttr = position.market.seconds_to_resolution()
    if ttr is not None and ttr < cfg.min_seconds_to_resolution:
        return ExitDecision(
            True, f"only {ttr:.0f}s to resolution -- never carry into settlement",
            "resolution_guard",
        )

    bid = book.best_bid
    if bid is None:
        # No bid means no exit liquidity. Say so loudly; do not pretend to exit.
        return ExitDecision(False, "no bid available -- cannot exit right now", "")

    move = bid - position.avg_price

    if move <= -cfg.stop_loss:
        return ExitDecision(
            True, f"stop loss: bid {bid:.3f} vs entry {position.avg_price:.3f} "
                  f"({move:+.3f} <= -{cfg.stop_loss:.3f})",
            "stop_loss",
        )

    if position.entry_signal is not None:
        target_move = (
            position.entry_signal.fair_value - position.avg_price
        ) * cfg.take_profit_edge_capture
        if target_move > 0 and move >= target_move:
            return ExitDecision(
                True,
                f"take profit: captured {move:+.3f} of target {target_move:+.3f} "
                f"(fair {position.entry_signal.fair_value:.3f})",
                "take_profit",
            )

    held = position.hold_seconds()
    if held >= cfg.max_hold_sec:
        return ExitDecision(
            True, f"time stop: held {held:.0f}s >= {cfg.max_hold_sec:.0f}s "
                  f"and the repricing never came",
            "time_stop",
        )

    return NO_EXIT

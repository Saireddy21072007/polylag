"""Plain data structures shared by every module.

Nothing in here talks to the network or the disk. Keeping the vocabulary of the
system in one dependency-free file makes the rest of the code easy to read and
easy to unit test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal, Optional

Side = Literal["BUY", "SELL"]
Outcome = Literal["YES", "NO"]


def now_ms() -> int:
    """Wall-clock milliseconds. Used for staleness checks and logs."""
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Market reference data
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MarketRef:
    """Everything we need to know about a market to trade it.

    Polymarket binary markets have two ERC-1155 outcome tokens whose prices sum
    to ~1.00. Buying NO is economically the same as selling YES, which matters a
    lot for exits (see execution/manager.py).
    """

    condition_id: str
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    end_date_iso: Optional[str] = None
    end_ts_ms: Optional[int] = None
    min_tick: float = 0.01
    min_order_size: float = 5.0
    accepting_orders: bool = True
    closed: bool = False

    def token_for(self, outcome: Outcome) -> str:
        return self.yes_token_id if outcome == "YES" else self.no_token_id

    def other_token(self, token_id: str) -> str:
        return self.no_token_id if token_id == self.yes_token_id else self.yes_token_id

    def outcome_for(self, token_id: str) -> Outcome:
        return "YES" if token_id == self.yes_token_id else "NO"

    def seconds_to_resolution(self) -> Optional[float]:
        if self.end_ts_ms is None:
            return None
        return (self.end_ts_ms - now_ms()) / 1000.0


# --------------------------------------------------------------------------- #
# Order book
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Level:
    price: float
    size: float  # shares


@dataclass
class OrderBook:
    """A snapshot of one outcome token's book.

    `bids` are sorted best (highest) first, `asks` best (lowest) first.
    """

    token_id: str
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)
    ts_ms: int = field(default_factory=now_ms)

    # -- simple accessors ---------------------------------------------------- #

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def age_ms(self) -> int:
        return now_ms() - self.ts_ms

    def is_crossed(self) -> bool:
        """A crossed/locked book means bad or stale data. Never trade on it."""
        if self.best_bid is None or self.best_ask is None:
            return False
        return self.best_bid >= self.best_ask

    # -- depth maths --------------------------------------------------------- #

    def depth_up_to(self, side: Side, limit_price: float) -> float:
        """Shares available at or better than `limit_price`."""
        if side == "BUY":
            return sum(lv.size for lv in self.asks if lv.price <= limit_price + 1e-12)
        return sum(lv.size for lv in self.bids if lv.price >= limit_price - 1e-12)

    def walk(self, side: Side, shares: float, limit_price: float) -> tuple[float, float]:
        """Consume the book for `shares`, never worse than `limit_price`.

        Returns (filled_shares, average_price). This is the single source of
        truth for what a marketable order would cost -- the paper broker and the
        pre-trade risk check both use it, so simulation and live sizing agree.
        """
        levels = self.asks if side == "BUY" else self.bids
        remaining, notional = shares, 0.0
        for lv in levels:
            if side == "BUY" and lv.price > limit_price + 1e-12:
                break
            if side == "SELL" and lv.price < limit_price - 1e-12:
                break
            take = min(remaining, lv.size)
            notional += take * lv.price
            remaining -= take
            if remaining <= 1e-9:
                break
        filled = shares - remaining
        avg = notional / filled if filled > 0 else 0.0
        return filled, avg


# --------------------------------------------------------------------------- #
# News
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NewsEvent:
    """One headline we have not seen before."""

    event_id: str  # stable hash of the link/title
    source: str
    title: str
    summary: str
    url: str
    published_ms: Optional[int]
    seen_ms: int = field(default_factory=now_ms)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.summary}"

    def age_sec(self) -> float:
        """Seconds since WE first saw it (not since publication).

        Publication timestamps in RSS are unreliable and often rounded to the
        minute, so the decision clock starts at `seen_ms`.
        """
        return (now_ms() - self.seen_ms) / 1000.0


# --------------------------------------------------------------------------- #
# Trading intents and results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Signal:
    """A fully explained reason to consider a trade. Always logged, even when
    it is rejected -- the rejection reasons are the most useful data you own."""

    market: MarketRef
    token_id: str
    outcome: Outcome
    side: Side
    reference_price: float  # price we would have to pay/receive now
    fair_value: float  # our transparent estimate
    edge: float  # fair_value - reference_price (BUY) / reverse for SELL
    trigger_name: str
    news_event_id: str
    news_title: str
    rationale: str
    ts_ms: int = field(default_factory=now_ms)


@dataclass(frozen=True)
class OrderIntent:
    market: MarketRef
    token_id: str
    side: Side
    shares: float
    limit_price: float
    reason: str
    signal: Optional[Signal] = None
    tag: str = "entry"  # entry | take_profit | stop_loss | time_stop | flatten


@dataclass(frozen=True)
class Fill:
    order_id: str
    token_id: str
    side: Side
    shares: float
    price: float
    fee_usdc: float
    ts_ms: int = field(default_factory=now_ms)


@dataclass
class Position:
    market: MarketRef
    token_id: str
    outcome: Outcome
    shares: float = 0.0
    avg_price: float = 0.0
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    opened_ms: int = field(default_factory=now_ms)
    entry_signal: Optional[Signal] = None
    peak_mark: float = 0.0

    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_price

    def hold_seconds(self) -> float:
        return (now_ms() - self.opened_ms) / 1000.0

    def unrealized(self, mark: float) -> float:
        return (mark - self.avg_price) * self.shares

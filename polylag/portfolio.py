"""Positions, cash and P&L.

Accounting rules used throughout:

  * Positions are LONG-ONLY in outcome tokens. To get short YES you buy NO.
    That keeps the accounting trivially correct (you can never owe more than
    you paid) and it is how the venue works anyway.
  * Open positions are marked at the BEST BID -- the price you could actually
    hit right now -- never at the mid. Mid-marking makes every strategy look
    better than it is.
  * Fees are subtracted at fill time and again on exit. They are never netted
    away or amortised, because fee drag is the single most common reason a
    "positive edge" strategy bleeds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import Fill, MarketRef, OrderBook, Position, Signal, now_ms

log = logging.getLogger("portfolio")


@dataclass
class ClosedTrade:
    market_slug: str
    outcome: str
    shares: float
    entry_price: float
    exit_price: float
    fees_usdc: float
    gross_pnl: float
    net_pnl: float
    hold_sec: float
    exit_reason: str
    trigger: str
    news_title: str
    opened_ms: int
    closed_ms: int = field(default_factory=now_ms)


class Portfolio:
    def __init__(self, starting_cash: float) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.total_fees = 0.0

    # -- queries ------------------------------------------------------------ #

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.shares > 1e-9]

    def position_for(self, token_id: str) -> Optional[Position]:
        pos = self.positions.get(token_id)
        return pos if pos and pos.shares > 1e-9 else None

    def exposure_in_market(self, condition_id: str) -> float:
        """Total cost basis across both legs of one market."""
        return sum(
            p.cost_basis
            for p in self.open_positions()
            if p.market.condition_id == condition_id
        )

    def gross_exposure(self) -> float:
        return sum(p.cost_basis for p in self.open_positions())

    def realized_pnl(self) -> float:
        return sum(t.net_pnl for t in self.closed_trades)

    def unrealized_pnl(self, books: dict[str, OrderBook]) -> float:
        total = 0.0
        for pos in self.open_positions():
            total += pos.unrealized(self._mark(pos, books))
        return total

    def _mark(self, pos: Position, books: dict[str, OrderBook]) -> float:
        """Conservative mark: best bid, else last known entry price.

        If the book is missing we fall back to cost, which means the position
        contributes zero unrealised P&L rather than a made-up number.
        """
        book = books.get(pos.token_id)
        if book is not None and book.best_bid is not None:
            return book.best_bid
        return pos.avg_price

    def equity(self, books: dict[str, OrderBook]) -> float:
        """Cash plus the liquidation value of open positions."""
        held = sum(
            pos.shares * self._mark(pos, books) for pos in self.open_positions()
        )
        return self.cash + held

    # -- mutations ---------------------------------------------------------- #

    def apply_buy(
        self, market: MarketRef, fill: Fill, signal: Optional[Signal] = None
    ) -> Position:
        cost = fill.shares * fill.price + fill.fee_usdc
        self.cash -= cost
        self.total_fees += fill.fee_usdc

        pos = self.positions.get(fill.token_id)
        if pos is None or pos.shares <= 1e-9:
            pos = Position(
                market=market,
                token_id=fill.token_id,
                outcome=market.outcome_for(fill.token_id),
                shares=fill.shares,
                avg_price=fill.price,
                fees_paid=fill.fee_usdc,
                entry_signal=signal,
            )
            self.positions[fill.token_id] = pos
        else:
            total_shares = pos.shares + fill.shares
            pos.avg_price = (
                pos.avg_price * pos.shares + fill.price * fill.shares
            ) / total_shares
            pos.shares = total_shares
            pos.fees_paid += fill.fee_usdc
        log.info(
            "BUY  %s %s %.2f sh @ %.3f (fee %.4f) -> cash %.2f",
            market.slug, pos.outcome, fill.shares, fill.price, fill.fee_usdc, self.cash,
        )
        return pos

    def apply_sell(self, fill: Fill, exit_reason: str) -> Optional[ClosedTrade]:
        pos = self.positions.get(fill.token_id)
        if pos is None or pos.shares <= 1e-9:
            log.error("sell fill for a token we do not hold: %s", fill.token_id[:12])
            return None

        shares = min(fill.shares, pos.shares)
        proceeds = shares * fill.price - fill.fee_usdc
        self.cash += proceeds
        self.total_fees += fill.fee_usdc

        gross = (fill.price - pos.avg_price) * shares
        entry_fee_share = pos.fees_paid * (shares / pos.shares) if pos.shares else 0.0
        net = gross - fill.fee_usdc - entry_fee_share

        pos.shares -= shares
        pos.fees_paid = max(0.0, pos.fees_paid - entry_fee_share)
        pos.realized_pnl += net

        trade = ClosedTrade(
            market_slug=pos.market.slug,
            outcome=pos.outcome,
            shares=shares,
            entry_price=pos.avg_price,
            exit_price=fill.price,
            fees_usdc=fill.fee_usdc + entry_fee_share,
            gross_pnl=gross,
            net_pnl=net,
            hold_sec=pos.hold_seconds(),
            exit_reason=exit_reason,
            trigger=pos.entry_signal.trigger_name if pos.entry_signal else "",
            news_title=pos.entry_signal.news_title if pos.entry_signal else "",
            opened_ms=pos.opened_ms,
        )
        self.closed_trades.append(trade)

        if pos.shares <= 1e-9:
            self.positions.pop(fill.token_id, None)

        log.info(
            "SELL %s %s %.2f sh @ %.3f -> net %+.3f (%s)",
            trade.market_slug, trade.outcome, shares, fill.price, net, exit_reason,
        )
        return trade

    def snapshot(self, books: dict[str, OrderBook]) -> dict:
        return {
            "cash": round(self.cash, 4),
            "equity": round(self.equity(books), 4),
            "open_positions": len(self.open_positions()),
            "gross_exposure": round(self.gross_exposure(), 4),
            "realized_pnl": round(self.realized_pnl(), 4),
            "unrealized_pnl": round(self.unrealized_pnl(books), 4),
            "total_fees": round(self.total_fees, 4),
            "closed_trades": len(self.closed_trades),
        }

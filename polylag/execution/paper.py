"""Paper broker: pessimistic fill simulation against the real live book.

A paper engine that fills you at the touch, instantly, for unlimited size, will
show you a beautiful equity curve and teach you nothing. This one deliberately
assumes you are the slowest participant in the room:

  1. LATENCY -- your order does not exist until `paper_latency_ms` have passed.
     We sleep, then re-read the book. If the market moved in that window, you
     get the new price or no fill at all. This alone kills most naive backtests.
  2. QUEUE/PARTICIPATION -- you can only take `paper_fill_participation` of the
     size displayed at each level. Displayed size is not all real, and other
     takers are hitting it at the same time as you.
  3. ADVERSE TICKS -- one extra tick of slippage on every fill, because the
     level you aimed at is usually the one that just disappeared.
  4. FEES -- charged with the same function the live path uses.

Even with all of that, paper results remain OPTIMISTIC. They cannot model your
own market impact, order rejections, or the fact that the counterparty who
filled you may have known something. Treat a profitable paper run as the
minimum bar for continuing, never as evidence of an edge.
"""

from __future__ import annotations

import asyncio
import itertools
import logging

from ..clients.ws import BookStore
from ..config import ExecutionConfig
from ..models import Fill, OrderIntent, now_ms
from .base import Broker, ExecutionResult, taker_fee

log = logging.getLogger("paper")


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, cfg: ExecutionConfig, store: BookStore, tick: float = 0.01) -> None:
        self.cfg = cfg
        self.store = store
        self.tick = tick
        self._ids = itertools.count(1)

    async def preflight(self) -> tuple[bool, str]:
        return True, "paper broker -- no funds at risk"

    async def buy(self, intent: OrderIntent) -> ExecutionResult:
        return await self._execute(intent, "BUY")

    async def sell(self, intent: OrderIntent) -> ExecutionResult:
        return await self._execute(intent, "SELL")

    async def _execute(self, intent: OrderIntent, side: str) -> ExecutionResult:
        order_id = f"paper-{next(self._ids):06d}"

        # 1. latency: the world keeps moving while your packet is in flight.
        await asyncio.sleep(self.cfg.paper_latency_ms / 1000.0)

        book = self.store.get(intent.token_id)
        if book is None:
            return ExecutionResult("rejected", "no book after latency window", order_id=order_id)
        if book.is_crossed():
            return ExecutionResult("rejected", "crossed book -- would not trade", order_id=order_id)

        # 3. adverse ticks: pay up (or receive less) versus your intended limit.
        slip = self.cfg.paper_adverse_ticks * self.tick
        effective_limit = (
            intent.limit_price - slip if side == "SELL" else intent.limit_price + slip
        )
        effective_limit = max(0.01, min(0.99, effective_limit))

        # 2. participation: only part of the displayed size is available to us.
        scaled = self._scaled_book(book, side)
        filled, avg_price = scaled.walk(side, intent.shares, effective_limit)  # type: ignore[arg-type]

        if filled <= 1e-9:
            return ExecutionResult(
                "rejected",
                f"no liquidity within limit {effective_limit:.3f} "
                f"(best {book.best_ask if side == 'BUY' else book.best_bid})",
                order_id=order_id,
            )

        fee = taker_fee(filled, avg_price, self.cfg.fee_bps)
        fill = Fill(
            order_id=order_id,
            token_id=intent.token_id,
            side=side,  # type: ignore[arg-type]
            shares=filled,
            price=avg_price,
            fee_usdc=fee,
            ts_ms=now_ms(),
        )
        status = "filled" if filled >= intent.shares - 1e-6 else "partial"
        log.debug(
            "paper %s %.2f/%.2f sh @ %.4f (limit %.3f, fee %.4f)",
            side, filled, intent.shares, avg_price, effective_limit, fee,
        )
        return ExecutionResult(
            status,  # type: ignore[arg-type]
            f"simulated {side} {filled:.2f} sh @ {avg_price:.4f}",
            fill=fill,
            order_id=order_id,
        )

    def _scaled_book(self, book, side: str):
        """A copy of the book with each level shrunk to our assumed share."""
        from ..models import Level, OrderBook  # local import keeps models dep-free

        factor = self.cfg.paper_fill_participation
        return OrderBook(
            token_id=book.token_id,
            bids=[Level(lv.price, lv.size * factor) for lv in book.bids],
            asks=[Level(lv.price, lv.size * factor) for lv in book.asks],
            ts_ms=book.ts_ms,
        )

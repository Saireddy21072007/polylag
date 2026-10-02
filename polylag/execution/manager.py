"""Order manager -- turns an approved signal into a position, and back out again.

It owns the awkward bits that neither the strategy nor the broker should know
about:

  * translating a Signal + a size into an OrderIntent
  * updating the portfolio only from CONFIRMED fills
  * refusing to continue when a fill is in an unknown state
  * the hedge fallback when a position cannot be sold

The hedge, explained
--------------------
YES and NO of the same market always settle to exactly 1.00 combined. So if you
hold 100 YES bought at 0.62 and the YES bid vanishes, you can buy 100 NO at,
say, 0.34 and your payoff is fixed: 100 * 1.00 = 100, against 96 spent. The
position is now risk-free and simply waits for resolution.

That is genuinely useful in one situation only: you need out and the book on
your own side is empty. It is not a way to "fix" a losing trade -- if YES+NO
costs you more than 1.00 you have locked in a loss, you have merely capped it.
We therefore only hedge when the locked-in loss is no worse than the stop loss
we were willing to take anyway.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..clients.ws import BookStore
from ..config import Config
from ..journal import Journal
from ..models import MarketRef, OrderIntent, Position, Signal
from ..portfolio import Portfolio
from ..risk.manager import RiskManager, SizedOrder
from .base import Broker, ExecutionResult

log = logging.getLogger("orders")


class UnknownFillState(RuntimeError):
    """Raised when the venue's answer leaves our position ambiguous.

    This is deliberately fatal. Trading on top of an unknown position is how a
    small problem becomes an account-sized one.
    """


class OrderManager:
    def __init__(
        self,
        cfg: Config,
        broker: Broker,
        portfolio: Portfolio,
        risk: RiskManager,
        store: BookStore,
        journal: Journal,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.portfolio = portfolio
        self.risk = risk
        self.store = store
        self.journal = journal

    # -- entries ------------------------------------------------------------ #

    async def open_position(
        self, signal: Signal, sized: SizedOrder, market: MarketRef
    ) -> Optional[Position]:
        intent = OrderIntent(
            market=market,
            token_id=signal.token_id,
            side="BUY",
            shares=sized.shares,
            limit_price=sized.limit_price,
            reason=signal.rationale,
            signal=signal,
            tag="entry",
        )
        self.journal.decision(
            "execution", "SUBMIT_ENTRY",
            f"{signal.rationale} | size capped by {sized.binding_constraint}",
            signal=signal, shares=sized.shares, limit_price=sized.limit_price,
        )

        result = await self.broker.buy(intent)
        self._guard_unknown(result, "entry")

        if not result.any_fill:
            self.journal.decision(
                "execution", "ENTRY_NOT_FILLED", result.message, signal=signal
            )
            log.info("entry not filled: %s", result.message)
            return None

        assert result.fill is not None
        position = self.portfolio.apply_buy(market, result.fill, signal)
        self.journal.fill(result.fill, market.slug, position.outcome, "entry")
        self.risk.record_entry()
        return position

    # -- exits -------------------------------------------------------------- #

    async def close_position(
        self, position: Position, reason: str, tag: str
    ) -> bool:
        """Sell the whole position. Falls back to hedging if we cannot sell."""
        book = self.store.get(position.token_id)
        if book is None:
            log.error("no book for %s -- cannot exit", position.token_id[:12])
            return False

        bid = book.best_bid
        if bid is not None:
            limit = max(0.01, round(bid - self.cfg.strategy.exit_slippage_allowance, 4))
            intent = OrderIntent(
                market=position.market,
                token_id=position.token_id,
                side="SELL",
                shares=position.shares,
                limit_price=limit,
                reason=reason,
                signal=position.entry_signal,
                tag=tag,
            )
            self.journal.decision(
                "execution", "SUBMIT_EXIT", f"{tag}: {reason}",
                signal=position.entry_signal, shares=position.shares, limit_price=limit,
            )
            result = await self.broker.sell(intent)
            self._guard_unknown(result, "exit")

            if result.any_fill:
                assert result.fill is not None
                self.journal.fill(result.fill, position.market.slug, position.outcome, tag)
                trade = self.portfolio.apply_sell(result.fill, reason)
                if trade is not None:
                    self.risk.record_close(trade.net_pnl)
                    self.journal.closed_trade(
                        {
                            "opened_ts": trade.opened_ms,
                            "closed_ts": trade.closed_ms,
                            "market_slug": trade.market_slug,
                            "outcome": trade.outcome,
                            "shares": round(trade.shares, 4),
                            "entry_price": round(trade.entry_price, 4),
                            "exit_price": round(trade.exit_price, 4),
                            "fees_usdc": round(trade.fees_usdc, 6),
                            "gross_pnl": round(trade.gross_pnl, 4),
                            "net_pnl": round(trade.net_pnl, 4),
                            "hold_sec": round(trade.hold_sec, 1),
                            "exit_reason": trade.exit_reason,
                            "trigger": trade.trigger,
                            "news_title": trade.news_title[:200],
                        }
                    )
                return True

            log.warning("exit sell did not fill (%s); considering hedge", result.message)

        return await self._hedge(position, reason)

    async def _hedge(self, position: Position, reason: str) -> bool:
        """Buy the opposite outcome to neutralise a position we cannot sell."""
        other_token = position.market.other_token(position.token_id)
        other_book = self.store.get(other_token)
        if other_book is None or other_book.best_ask is None:
            log.error("cannot hedge %s: no opposite book", position.market.slug)
            self.journal.decision(
                "execution", "HEDGE_IMPOSSIBLE",
                "no liquidity on either leg -- position remains open",
                signal=position.entry_signal,
            )
            return False

        hedge_price = other_book.best_ask
        combined_cost = position.avg_price + hedge_price
        locked_loss = combined_cost - 1.0  # per share; negative means locked profit
        worst_acceptable = self.cfg.strategy.stop_loss

        if locked_loss > worst_acceptable:
            self.journal.decision(
                "execution", "HEDGE_REJECTED",
                f"hedge at {hedge_price:.3f} would lock {locked_loss:+.3f}/share, "
                f"worse than stop loss {worst_acceptable:.3f}",
                signal=position.entry_signal,
            )
            log.warning("hedge too expensive (%.3f/share locked); holding", locked_loss)
            return False

        intent = OrderIntent(
            market=position.market,
            token_id=other_token,
            side="BUY",
            shares=position.shares,
            limit_price=min(0.99, round(hedge_price + position.market.min_tick, 4)),
            reason=f"hedge: {reason}",
            signal=position.entry_signal,
            tag="hedge",
        )
        self.journal.decision(
            "execution", "SUBMIT_HEDGE",
            f"locking {locked_loss:+.3f}/share by buying the opposite leg",
            signal=position.entry_signal, shares=position.shares,
            limit_price=intent.limit_price,
        )
        result = await self.broker.buy(intent)
        self._guard_unknown(result, "hedge")

        if result.any_fill:
            assert result.fill is not None
            self.portfolio.apply_buy(position.market, result.fill, position.entry_signal)
            self.journal.fill(
                result.fill, position.market.slug,
                position.market.outcome_for(other_token), "hedge",
            )
            log.info(
                "hedged %s: %.2f sh of the opposite leg, payoff now fixed",
                position.market.slug, result.fill.shares,
            )
            return True

        log.error("hedge failed: %s", result.message)
        return False

    async def flatten_all(self, reason: str) -> None:
        """Close everything. Used by the kill switch and clean shutdown."""
        for position in list(self.portfolio.open_positions()):
            try:
                await self.close_position(position, reason, "flatten")
            except UnknownFillState:
                raise
            except Exception:  # noqa: BLE001 - keep trying the other positions
                log.exception("failed to flatten %s", position.market.slug)
        await self.broker.cancel_all()

    # -- safety ------------------------------------------------------------- #

    def _guard_unknown(self, result: ExecutionResult, phase: str) -> None:
        if result.status == "unknown":
            message = (
                f"{phase} order returned an UNKNOWN state ({result.message}). "
                "Positions may not match our records. Stopping and latching the "
                "kill switch -- reconcile manually on Polymarket before restarting."
            )
            self.journal.decision("execution", "UNKNOWN_FILL", message)
            self.risk.kill(message)
            raise UnknownFillState(message)

"""The risk manager -- the most important file in the repository.

Three layers, in increasing severity:

  1. Per-trade gates      reject one order, keep running
  2. Daily halt           stop opening positions until the next UTC day
  3. Kill latch           flatten everything, write state/KILL, refuse to
                          restart until a human deletes the file

Design rules that must not be relaxed:

  * The risk manager is the ONLY thing that authorises an order, and it is
    consulted for exits too (an exit is always allowed; that is the asymmetry).
  * Every limit is checked against LIVE equity, recomputed from the book, not
    against a number cached at startup.
  * All counters that matter (peak equity, day anchor, kill latch) live on disk.
    Restarting the process must never hand you a fresh drawdown allowance.
  * When any input is missing or stale, the answer is NO. Silence is not
    permission.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..config import Config
from ..journal import StateStore
from ..models import MarketRef, OrderBook, Signal, now_ms
from ..portfolio import Portfolio

log = logging.getLogger("risk")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    gate: str = ""

    @staticmethod
    def no(gate: str, reason: str) -> "Verdict":
        return Verdict(False, reason, gate)

    @staticmethod
    def yes(reason: str = "ok") -> "Verdict":
        return Verdict(True, reason, "")


@dataclass(frozen=True)
class SizedOrder:
    shares: float
    limit_price: float
    notional: float
    binding_constraint: str


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def sizing_feasible(limits, equity: float, min_order_size: float) -> tuple[bool, str]:
    """Can this bankroll place a legal order under these caps?

    Worth checking explicitly at startup: with a $200 bankroll and a 1% cap you
    can only ever size a $2 order, and the venue's minimum is around $5. Nothing
    would error -- every single signal would just be silently rejected as
    below-minimum, and you would conclude the strategy never triggers.

    The 1.2x headroom covers the rounding-down to whole shares.
    """
    per_trade_cap = min(
        equity * limits.max_trade_pct_of_equity / 100.0,
        limits.max_notional_per_trade_usdc,
    )
    required = min_order_size * 1.2
    if per_trade_cap >= required:
        return True, f"per-trade cap ${per_trade_cap:.2f} vs venue min ${min_order_size:.2f}"

    needed_equity = required * 100.0 / limits.max_trade_pct_of_equity
    return False, (
        f"per-trade cap is ${per_trade_cap:.2f} but the venue minimum is "
        f"${min_order_size:.2f}. Every order would be rejected as too small. "
        f"Either fund at least ${needed_equity:.0f}, or raise "
        f"max_trade_pct_of_equity above "
        f"{required / equity * 100:.2f}% (which means more risk per trade)."
    )


class RiskManager:
    def __init__(self, cfg: Config, state: StateStore, portfolio: Portfolio) -> None:
        self.cfg = cfg
        self.limits = cfg.risk
        self.state = state
        self.portfolio = portfolio

        self._trade_times: deque[int] = deque()  # ms timestamps of entries
        self._trades_today = 0
        self._consecutive_losses = 0
        self._last_trade_ms = 0
        self._loss_pause_until_ms = 0
        self.day_halt_reason = ""

        equity = portfolio.equity({})
        self.peak_equity = float(state.get("peak_equity", equity))
        self.day_key = str(state.get("day_key", _utc_day()))
        self.day_start_equity = float(state.get("day_start_equity", equity))
        self._trades_today = int(state.get("trades_today", 0))
        self._roll_day_if_needed(equity)

    # -- persistence -------------------------------------------------------- #

    def _persist(self) -> None:
        self.state.data.update(
            peak_equity=round(self.peak_equity, 6),
            day_key=self.day_key,
            day_start_equity=round(self.day_start_equity, 6),
            trades_today=self._trades_today,
            updated=datetime.now(timezone.utc).isoformat(),
        )
        self.state.save()

    def _roll_day_if_needed(self, equity: float) -> None:
        today = _utc_day()
        if today != self.day_key:
            log.info(
                "new UTC day %s -> day anchor equity %.2f (previous day %s)",
                today, equity, self.day_key,
            )
            self.day_key = today
            self.day_start_equity = equity
            self._trades_today = 0
            self.day_halt_reason = ""
            self._persist()

    # -- continuous monitoring ---------------------------------------------- #

    def update_equity(self, equity: float) -> Verdict:
        """Call this every heartbeat. Escalates to halts and the kill latch.

        Returns a Verdict describing the current trading permission.
        """
        self._roll_day_if_needed(equity)

        if equity > self.peak_equity:
            self.peak_equity = equity
            self._persist()

        # Layer 3: permanent kill conditions -------------------------------- #
        if equity <= self.limits.min_equity_floor_usdc:
            self._kill(
                f"equity {equity:.2f} at or below floor "
                f"{self.limits.min_equity_floor_usdc:.2f}"
            )
            return Verdict.no("equity_floor", "equity floor breached -- killed")

        drawdown_pct = (
            (self.peak_equity - equity) / self.peak_equity * 100.0
            if self.peak_equity > 0 else 0.0
        )
        if drawdown_pct >= self.limits.max_drawdown_pct:
            self._kill(
                f"drawdown {drawdown_pct:.2f}% from peak {self.peak_equity:.2f} "
                f">= limit {self.limits.max_drawdown_pct:.2f}%"
            )
            return Verdict.no("max_drawdown", "max drawdown breached -- killed")

        # Layer 2: daily halt ------------------------------------------------ #
        day_loss_pct = (
            (self.day_start_equity - equity) / self.day_start_equity * 100.0
            if self.day_start_equity > 0 else 0.0
        )
        if day_loss_pct >= self.limits.daily_loss_limit_pct:
            if not self.day_halt_reason:
                self.day_halt_reason = (
                    f"daily loss {day_loss_pct:.2f}% >= limit "
                    f"{self.limits.daily_loss_limit_pct:.2f}%"
                )
                log.critical("DAILY HALT: %s", self.day_halt_reason)
            return Verdict.no("daily_loss", self.day_halt_reason)

        return Verdict.yes()

    def _kill(self, reason: str) -> None:
        if not self.state.is_killed():
            self.state.engage_kill(reason)

    def kill(self, reason: str) -> None:
        """Manual kill (CLI, signal handler, unhandled exception)."""
        self._kill(reason)

    def is_killed(self) -> bool:
        return self.state.is_killed()

    def status(self, equity: float) -> dict:
        drawdown_pct = (
            (self.peak_equity - equity) / self.peak_equity * 100.0
            if self.peak_equity > 0 else 0.0
        )
        day_loss_pct = (
            (self.day_start_equity - equity) / self.day_start_equity * 100.0
            if self.day_start_equity > 0 else 0.0
        )
        return {
            "equity": round(equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown_pct": round(drawdown_pct, 3),
            "drawdown_limit_pct": self.limits.max_drawdown_pct,
            "day_start_equity": round(self.day_start_equity, 2),
            "day_loss_pct": round(day_loss_pct, 3),
            "daily_limit_pct": self.limits.daily_loss_limit_pct,
            "trades_today": self._trades_today,
            "trades_last_hour": len(self._recent_trades()),
            "consecutive_losses": self._consecutive_losses,
            "killed": self.state.is_killed(),
            "kill_reason": self.state.kill_reason(),
            "day_halted": bool(self.day_halt_reason),
        }

    # -- pre-trade gates ---------------------------------------------------- #

    def _recent_trades(self) -> deque[int]:
        cutoff = now_ms() - 3_600_000
        while self._trade_times and self._trade_times[0] < cutoff:
            self._trade_times.popleft()
        return self._trade_times

    def can_open(
        self,
        signal: Signal,
        book: OrderBook,
        equity: float,
        feed_connected: bool,
        news_age_sec: float,
    ) -> Verdict:
        """Everything that can stop a NEW position. Exits bypass this."""
        limits = self.limits

        if self.state.is_killed():
            return Verdict.no("kill_switch", f"kill latch set: {self.state.kill_reason()}")

        permission = self.update_equity(equity)
        if not permission.ok:
            return permission

        if not feed_connected:
            return Verdict.no("feed_health", "market data feed is disconnected")

        if book.age_ms() > limits.max_book_staleness_ms:
            return Verdict.no(
                "stale_book",
                f"book {book.age_ms()}ms old > {limits.max_book_staleness_ms}ms",
            )

        if news_age_sec > limits.max_news_age_sec:
            return Verdict.no(
                "stale_news",
                f"headline is {news_age_sec:.0f}s old > {limits.max_news_age_sec:.0f}s "
                "-- the window has closed",
            )

        if len(self.portfolio.open_positions()) >= limits.max_open_positions:
            return Verdict.no(
                "max_positions",
                f"{len(self.portfolio.open_positions())} open >= limit "
                f"{limits.max_open_positions}",
            )

        if self.portfolio.position_for(signal.token_id) is not None:
            return Verdict.no("already_long", "already holding this outcome token")

        opposite = signal.market.other_token(signal.token_id)
        if self.portfolio.position_for(opposite) is not None:
            return Verdict.no(
                "opposite_leg",
                "holding the opposite outcome -- would create a self-hedged wash",
            )

        now = now_ms()
        if now < self._loss_pause_until_ms:
            remaining = (self._loss_pause_until_ms - now) / 1000
            return Verdict.no(
                "loss_streak",
                f"{self._consecutive_losses} consecutive losses -- paused "
                f"{remaining:.0f}s more",
            )

        if now - self._last_trade_ms < limits.cooldown_after_trade_sec * 1000:
            wait = limits.cooldown_after_trade_sec - (now - self._last_trade_ms) / 1000
            return Verdict.no("cooldown", f"cooldown active, {wait:.0f}s remaining")

        if len(self._recent_trades()) >= limits.max_new_trades_per_hour:
            return Verdict.no(
                "hourly_throttle",
                f"{len(self._recent_trades())} trades this hour >= limit "
                f"{limits.max_new_trades_per_hour}",
            )

        if self._trades_today >= limits.max_new_trades_per_day:
            return Verdict.no(
                "daily_throttle",
                f"{self._trades_today} trades today >= limit "
                f"{limits.max_new_trades_per_day}",
            )

        if self.portfolio.gross_exposure() >= equity * limits.max_gross_exposure_pct / 100:
            return Verdict.no(
                "gross_exposure",
                f"gross exposure {self.portfolio.gross_exposure():.2f} >= "
                f"{limits.max_gross_exposure_pct:.1f}% of equity",
            )

        return Verdict.yes()

    def size_order(
        self,
        signal: Signal,
        market: MarketRef,
        book: OrderBook,
        equity: float,
    ) -> tuple[Optional[SizedOrder], Verdict]:
        """Smallest of every cap wins. The binding constraint is logged so you
        can see which limit is actually shaping your trades."""
        limits = self.limits
        ask = book.best_ask
        if ask is None:
            return None, Verdict.no("no_ask", "no ask to buy against")

        # Cross at most one tick beyond the touch. We are trying to be early,
        # not to pay whatever the book asks.
        limit_price = min(0.99, round(ask + market.min_tick, 4))

        caps: list[tuple[str, float]] = [
            ("pct_of_equity", equity * limits.max_trade_pct_of_equity / 100.0),
            ("per_trade_cap", limits.max_notional_per_trade_usdc),
            (
                "per_market_cap",
                limits.max_notional_per_market_usdc
                - self.portfolio.exposure_in_market(market.condition_id),
            ),
            (
                "gross_exposure_cap",
                equity * limits.max_gross_exposure_pct / 100.0
                - self.portfolio.gross_exposure(),
            ),
            ("cash", max(0.0, self.portfolio.cash * 0.98)),  # leave room for fees
        ]
        binding, notional = min(caps, key=lambda kv: kv[1])
        if notional <= 0:
            return None, Verdict.no("no_capacity", f"{binding} leaves no room")

        shares = notional / limit_price

        # Never take more than a slice of visible depth: our own market impact
        # is a cost we control.
        available = book.depth_up_to("BUY", limit_price)
        depth_cap = available * limits.max_depth_participation
        if depth_cap < shares:
            shares, binding = depth_cap, "depth_participation"

        shares = float(int(shares))  # whole shares keep the maths honest
        if shares * limit_price < market.min_order_size:
            return None, Verdict.no(
                "below_min_size",
                f"sized to ${shares * limit_price:.2f}, below venue minimum "
                f"${market.min_order_size:.2f} (binding cap: {binding})",
            )

        return (
            SizedOrder(
                shares=shares,
                limit_price=limit_price,
                notional=shares * limit_price,
                binding_constraint=binding,
            ),
            Verdict.yes(f"binding constraint: {binding}"),
        )

    # -- post-trade bookkeeping --------------------------------------------- #

    def record_entry(self) -> None:
        now = now_ms()
        self._trade_times.append(now)
        self._trades_today += 1
        self._last_trade_ms = now
        self._persist()

    def record_close(self, net_pnl: float) -> None:
        if net_pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self.limits.loss_streak_pause:
                self._loss_pause_until_ms = (
                    now_ms() + self.limits.loss_streak_cooldown_sec * 1000
                )
                log.warning(
                    "%d consecutive losses -- pausing new entries for %ds",
                    self._consecutive_losses, self.limits.loss_streak_cooldown_sec,
                )
        else:
            self._consecutive_losses = 0
        self._persist()

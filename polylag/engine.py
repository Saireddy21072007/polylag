"""The engine: wires every component together and owns the run loop.

Data flow (also drawn in README.md):

    RSS feeds ──► NewsMonitor ──► TriggerMatcher ──► rules.evaluate_entry
                                                          │
    CLOB websocket ──► BookStore ─────────────────────────┤
                                                          ▼
                                                   RiskManager.can_open
                                                          │  (+ size_order)
                                                          ▼
                                                    OrderManager
                                                     │        │
                                              PaperBroker  LiveBroker
                                                     └───┬────┘
                                                         ▼
                                                     Portfolio ──► Journal
                                                         │
                                     position loop ◄─────┘ (exits, marks, equity)
                                                         │
                                                    RiskManager.update_equity
                                                         │
                                                    kill switch / halt

Two independent clocks run at all times:
  * news-driven, event-based: entries only
  * a 1-second position loop: exits, marking, and risk escalation

Exits never wait for news. That asymmetry is intentional -- getting out must
never depend on the same machinery that got you in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .clients.clob import ClobReadClient
from .clients.gamma import GammaClient
from .clients.ws import BookStore, MarketFeed
from .config import Config
from .execution.base import Broker
from .execution.live import LiveBroker
from .execution.manager import OrderManager, UnknownFillState
from .execution.paper import PaperBroker
from .journal import Journal, StateStore
from .logging_setup import log_event
from .models import MarketRef, NewsEvent
from .news.feeds import NewsMonitor
from .news.matcher import TriggerMatcher
from .portfolio import Portfolio
from .risk.manager import RiskManager, sizing_feasible
from .strategy import rules

log = logging.getLogger("engine")

# How far before the headline to take the reference mid. A couple of seconds of
# slack keeps a same-second move from contaminating the "before" price.
PRE_NEWS_LOOKBACK_MS = 2000

POSITION_LOOP_SEC = 1.0
HEARTBEAT_SEC = 30.0


@dataclass
class Overrides:
    """Injection points used by the offline simulator (see simulate.py).

    Production runs pass nothing here. The simulator swaps the two network
    edges -- market data and news -- for scripted versions, and everything in
    between is the same code that trades real money. That is the point: a
    simulation that exercises a parallel implementation proves nothing.
    """

    markets: Optional[dict[str, MarketRef]] = None
    news: Optional[Any] = None
    feed_factory: Optional[Callable[[BookStore], Any]] = None
    skip_health_check: bool = False
    install_signal_handlers: bool = True


class Engine:
    def __init__(
        self, cfg: Config, mode: str, overrides: Optional[Overrides] = None
    ) -> None:
        if mode not in ("watch", "paper", "live"):
            raise ValueError("mode must be watch, paper or live")
        self.cfg = cfg
        self.mode = mode
        self.run_id = uuid.uuid4().hex[:8]
        self.overrides = overrides or Overrides()

        self.state = StateStore(cfg.state_dir)
        self.journal = Journal(cfg.log_dir, self.run_id, mode)
        self.store = BookStore()
        self.gamma = GammaClient(
            cfg.endpoints.gamma, cfg.endpoints.http_timeout_sec,
            cfg.endpoints.rate_limit_per_sec, cfg.endpoints.rate_limit_burst,
            proxy=cfg.endpoints.proxy_url,
        )
        self.clob = ClobReadClient(
            cfg.endpoints.clob, cfg.endpoints.http_timeout_sec,
            cfg.endpoints.rate_limit_per_sec, cfg.endpoints.rate_limit_burst,
            proxy=cfg.endpoints.proxy_url,
        )
        self.feed = (
            self.overrides.feed_factory(self.store)
            if self.overrides.feed_factory
            else MarketFeed(
                cfg.endpoints.clob_ws, self.clob, self.store,
                proxy=cfg.endpoints.proxy_url,
            )
        )
        self.news = self.overrides.news or NewsMonitor(
            cfg.news, cfg.endpoints.http_timeout_sec, proxy=cfg.endpoints.proxy_url
        )
        self.matcher = TriggerMatcher(cfg.markets)

        self.markets: dict[str, MarketRef] = {}  # slug -> ref
        self.portfolio: Optional[Portfolio] = None
        self.risk: Optional[RiskManager] = None
        self.broker: Optional[Broker] = None
        self.orders: Optional[OrderManager] = None

        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._entry_lock = asyncio.Lock()  # one entry decision at a time
        self._torn_down = False
        self._interrupts = 0

    # -- setup -------------------------------------------------------------- #

    async def setup(self) -> None:
        log.info("run %s starting in %s mode", self.run_id, self.mode.upper())

        if self.state.is_killed():
            raise SystemExit(
                f"kill switch is latched: {self.state.kill_reason()}\n"
                f"Review what happened, then delete {self.state.kill_path} to resume."
            )

        if not self.overrides.skip_health_check and not await self.clob.health():
            raise SystemExit(
                "CLOB is unreachable -- refusing to start.\n"
                "Check your connection first: curl https://clob.polymarket.com/ok\n"
                "If your network blocks it, set endpoints.proxy_url in config.yaml."
            )

        await self._resolve_markets()
        await self._start_broker()

        assert self.broker is not None and self.portfolio is not None
        self._check_sizing_feasible()
        self.risk = RiskManager(self.cfg, self.state, self.portfolio)
        self.orders = OrderManager(
            self.cfg, self.broker, self.portfolio, self.risk, self.store, self.journal
        )

        token_ids = [t for m in self.markets.values() for t in (m.yes_token_id, m.no_token_id)]
        await self.feed.start(token_ids)
        await self.news.prime()

        log_event(
            log, logging.INFO, "engine ready",
            run_id=self.run_id, mode=self.mode,
            markets=list(self.markets), tokens=len(token_ids),
            equity=self.portfolio.equity({}),
        )

    async def _resolve_markets(self) -> None:
        if self.overrides.markets is not None:
            self.markets = dict(self.overrides.markets)
            log.info("using %d injected market(s)", len(self.markets))
            return
        for market_cfg in self.cfg.enabled_markets():
            ref = await self.gamma.resolve_slug(market_cfg.slug)
            if ref is None:
                log.error("skipping %s -- could not resolve", market_cfg.slug)
                continue
            ttr = ref.seconds_to_resolution()
            if ttr is not None and ttr < self.cfg.strategy.min_seconds_to_resolution:
                log.warning(
                    "skipping %s -- resolves in %.0fs, inside our guard window",
                    market_cfg.slug, ttr,
                )
                continue
            self.markets[market_cfg.slug] = ref
            log.info("watching %s (%s)", market_cfg.slug, ref.question[:70])
        if not self.markets:
            raise SystemExit("no tradable markets resolved -- check config.yaml slugs")

    def _check_sizing_feasible(self) -> None:
        """Refuse to run a configuration that can never place an order."""
        assert self.portfolio is not None
        equity = self.portfolio.equity({})
        worst_min = max(m.min_order_size for m in self.markets.values())
        ok, message = sizing_feasible(self.cfg.risk, equity, worst_min)
        if not ok:
            raise SystemExit(f"sizing is impossible with this configuration.\n{message}")
        log.info("sizing check: %s", message)

    async def _start_broker(self) -> None:
        if self.mode == "live":
            broker = LiveBroker(self.cfg)
            await broker.connect()
            ok, message = await broker.preflight()
            if not ok:
                raise SystemExit(f"live preflight failed: {message}")
            log.warning("LIVE MODE ACTIVE -- %s", message)
            balance = await broker.collateral_balance() or 0.0
            self.portfolio = Portfolio(starting_cash=balance)
            self.broker = broker
        else:
            tick = min((m.min_tick for m in self.markets.values()), default=0.01)
            self.broker = PaperBroker(self.cfg.execution, self.store, tick)
            self.portfolio = Portfolio(starting_cash=self.cfg.risk.starting_bankroll_usdc)

    # -- news path ---------------------------------------------------------- #

    async def on_news(self, event: NewsEvent) -> None:
        matches = self.matcher.match(event)
        if not matches:
            return

        async with self._entry_lock:  # never evaluate two entries concurrently
            for match in matches:
                try:
                    await self._consider(match, event)
                except UnknownFillState:
                    await self.shutdown("unknown fill state")
                    return
                except Exception:  # noqa: BLE001 - one bad market must not stop us
                    log.exception("error considering %s", match.market.slug)

    async def _consider(self, match, event: NewsEvent) -> None:
        assert self.risk and self.portfolio and self.orders

        market = self.markets.get(match.market.slug)
        if market is None:
            return
        token_id = market.token_for(match.trigger.outcome)  # type: ignore[arg-type]
        book = self.store.get(token_id)
        if book is None:
            self.journal.decision("signal", "REJECT", "no book for token yet")
            return

        anchor = self.store.mid_at_or_before(token_id, event.seen_ms - PRE_NEWS_LOOKBACK_MS)

        evaluation = rules.evaluate_entry(
            match=match,
            event=event,
            market=market,
            book=book,
            pre_news_mid=anchor,
            cfg=self.cfg.strategy,
            max_book_staleness_ms=self.cfg.risk.max_book_staleness_ms,
            max_spread=self.cfg.risk.max_spread,
            min_depth_shares=self.cfg.risk.min_top_of_book_depth_shares,
        )
        if not evaluation.accepted:
            self.journal.decision(
                "signal", "REJECT", f"[{evaluation.gate}] {evaluation.reason}"
            )
            log.info("reject %s/%s: %s", match.market.slug, evaluation.gate, evaluation.reason)
            return

        signal_obj = evaluation.signal
        assert signal_obj is not None
        self.journal.decision("signal", "ACCEPT", evaluation.reason, signal=signal_obj)
        log.info("SIGNAL %s", signal_obj.rationale)

        books = self._books_for_marking()
        equity = self.portfolio.equity(books)

        verdict = self.risk.can_open(
            signal_obj, book, equity, self.store.connected, event.age_sec()
        )
        if not verdict.ok:
            self.journal.decision(
                "risk", "REJECT", f"[{verdict.gate}] {verdict.reason}", signal=signal_obj
            )
            log.warning("risk blocked entry: %s", verdict.reason)
            return

        sized, size_verdict = self.risk.size_order(signal_obj, market, book, equity)
        if sized is None:
            self.journal.decision(
                "risk", "REJECT", f"[{size_verdict.gate}] {size_verdict.reason}",
                signal=signal_obj,
            )
            log.warning("sizing blocked entry: %s", size_verdict.reason)
            return

        if self.mode == "watch":
            self.journal.decision(
                "execution", "WOULD_TRADE",
                f"watch mode -- would buy {sized.shares:.0f} sh @ {sized.limit_price:.3f} "
                f"(${sized.notional:.2f}, capped by {sized.binding_constraint})",
                signal=signal_obj, shares=sized.shares, limit_price=sized.limit_price,
            )
            log.warning(
                "WATCH MODE: would buy %.0f sh of %s @ %.3f ($%.2f)",
                sized.shares, signal_obj.outcome, sized.limit_price, sized.notional,
            )
            return

        await self.orders.open_position(signal_obj, sized, market)

    # -- position loop ------------------------------------------------------ #

    def _books_for_marking(self) -> dict:
        books = {}
        for market in self.markets.values():
            for token in (market.yes_token_id, market.no_token_id):
                book = self.store.get(token)
                if book is not None:
                    books[token] = book
        return books

    async def _position_loop(self) -> None:
        assert self.risk and self.portfolio and self.orders
        while not self._stop.is_set():
            try:
                books = self._books_for_marking()
                equity = self.portfolio.equity(books)
                verdict = self.risk.update_equity(equity)

                if self.risk.is_killed():
                    log.critical("kill switch active -- flattening and stopping")
                    if self.mode != "watch":
                        await self.orders.flatten_all("kill switch")
                    await self.shutdown("kill switch")
                    return

                for position in list(self.portfolio.open_positions()):
                    book = self.store.get(position.token_id)
                    if book is None:
                        continue
                    decision = rules.evaluate_exit(position, book, self.cfg.strategy)
                    if decision.should_exit and self.mode != "watch":
                        log.info("EXIT %s: %s", position.market.slug, decision.reason)
                        self.journal.decision(
                            "exit", decision.tag.upper(), decision.reason,
                            signal=position.entry_signal,
                        )
                        await self.orders.close_position(
                            position, decision.reason, decision.tag
                        )

                if not verdict.ok and not self.portfolio.open_positions():
                    log.warning("trading halted (%s); positions are flat", verdict.reason)
            except UnknownFillState:
                await self.shutdown("unknown fill state")
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("position loop error")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POSITION_LOOP_SEC)
                return
            except asyncio.TimeoutError:
                continue

    async def _heartbeat(self) -> None:
        assert self.risk and self.portfolio
        while not self._stop.is_set():
            try:
                await self._heartbeat_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reporting must never kill the run
                log.exception("heartbeat failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_SEC)
                return
            except asyncio.TimeoutError:
                continue

    async def _heartbeat_once(self) -> None:
        assert self.risk and self.portfolio
        books = self._books_for_marking()
        snapshot = self.portfolio.snapshot(books)
        status = self.risk.status(snapshot["equity"])
        # snapshot and status deliberately overlap (both carry `equity`), so
        # merge into one dict rather than splatting two into keyword args --
        # that raised TypeError and silently killed this task 30s into a run.
        payload = {
            "run_id": self.run_id,
            "mode": self.mode,
            "ws_connected": self.store.connected,
            "books": len(books),
            "news_seen": self.news.events_seen,
            **snapshot,
            **status,
        }
        log_event(log, logging.INFO, "heartbeat", **payload)
        log.info(
            "equity %.2f | dd %.2f%%/%.2f%% | day %.2f%%/%.2f%% | open %d | "
            "trades today %d | ws %s",
            status["equity"], status["drawdown_pct"], status["drawdown_limit_pct"],
            status["day_loss_pct"], status["daily_limit_pct"],
            snapshot["open_positions"], status["trades_today"],
            "up" if self.store.connected else "DOWN",
        )

    # -- lifecycle ---------------------------------------------------------- #

    async def run(self) -> None:
        await self.setup()
        if self.overrides.install_signal_handlers:
            self._install_signal_handlers()

        await self.news.start(self.on_news)
        self._tasks = [
            asyncio.create_task(self._position_loop(), name="positions"),
            asyncio.create_task(self._heartbeat(), name="heartbeat"),
        ]
        log.info("running -- Ctrl-C to stop cleanly")
        await self._stop.wait()
        await self._teardown()

    def _install_signal_handlers(self) -> None:
        """Make Ctrl-C a clean, position-flattening shutdown on every platform.

        Windows has no `loop.add_signal_handler`, and letting KeyboardInterrupt
        propagate tears the event loop down *before* the flatten runs -- which
        would leave real positions open with nobody watching. So we install a
        plain signal handler that sets the stop event thread-safely and let the
        normal teardown path do its job.

        A second Ctrl-C aborts immediately, on the assumption that you have a
        good reason and will reconcile by hand.
        """
        loop = asyncio.get_running_loop()

        def request_stop(*_: Any) -> None:
            self._interrupts += 1
            if self._interrupts >= 2:
                log.critical("second interrupt -- aborting NOW without flattening")
                log.critical("check your open positions on Polymarket manually")
                os._exit(130)
            print("\nstopping: flattening positions, press Ctrl-C again to abort", flush=True)
            loop.call_soon_threadsafe(self._stop.set)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_stop)
            except (NotImplementedError, AttributeError):
                try:
                    signal.signal(sig, request_stop)
                except (OSError, ValueError):
                    log.warning("could not install a handler for %s", sig)

    async def shutdown(self, reason: str) -> None:
        if self._stop.is_set():
            return
        log.warning("shutting down: %s", reason)
        self._stop.set()

    async def _teardown(self) -> None:
        if self._torn_down:  # shutdown can be requested from several places
            return
        self._torn_down = True

        # An unattended bot must not leave positions open behind it. Whoever is
        # not watching the screen cannot manage a position.
        if self.orders and self.portfolio and self.mode != "watch":
            if self.portfolio.open_positions():
                log.warning("flattening %d open position(s) before exit",
                            len(self.portfolio.open_positions()))
                with contextlib.suppress(Exception):
                    await self.orders.flatten_all("shutdown")

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - report, never mask, a dead loop
                log.exception("task %s died during the run", task.get_name())

        await self.news.stop()
        await self.feed.stop()
        if self.broker:
            await self.broker.close()
        await self.clob.close()
        await self.gamma.close()

        if self.portfolio and self.risk:
            books = self._books_for_marking()
            snapshot = self.portfolio.snapshot(books)
            log_event(log, logging.INFO, "final", run_id=self.run_id, **snapshot)
            log.info("final: %s", snapshot)

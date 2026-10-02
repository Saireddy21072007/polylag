"""Offline end-to-end simulator.

Why this exists
---------------
You cannot prove a trading system works by unit-testing its parts. You also
cannot prove it by trading real money -- that is the expensive way to find out.
This module runs the ENTIRE engine against a scripted market and scripted
headlines, with only the two network edges replaced:

    replaced:  the CLOB websocket  ->  ScriptedFeed (writes books on a timeline)
               the RSS feeds       ->  ScriptedNews (emits one headline)

    REAL:      matcher, fair value, every entry gate, the risk manager, sizing,
               the order manager, the paper broker's fill simulation, the
               portfolio accounting, the exit rules, the journal, the kill
               switch, the shutdown-flatten path.

So a scenario that ends in a `take_profit` exit has genuinely exercised the same
code path that would have spent your money. A simulator that reimplemented any
of that would prove nothing about the real thing.

What it is NOT
--------------
It cannot validate the Gamma/CLOB response parsing (nothing here talks to them),
and its price paths are made up by me, not sampled from real market behaviour.
**A green simulation says the machinery works. It says nothing whatsoever about
whether the strategy makes money.**

Run it:  python run.py simulate            (all scenarios)
         python run.py simulate --scenario clean-lag
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .clients.ws import BookStore
from .config import Config, MarketConfig, TriggerRule
from .engine import Engine, Overrides
from .models import Level, MarketRef, NewsEvent, OrderBook, now_ms

log = logging.getLogger("simulate")

SIM_MARKET_SLUG = "sim-fed-cut"
SIM_YES = "SIM_TOKEN_YES"
SIM_NO = "SIM_TOKEN_NO"


# --------------------------------------------------------------------------- #
# Scenario definition
# --------------------------------------------------------------------------- #


@dataclass
class Scenario:
    """One scripted story: a market, a headline, and what the price does next."""

    name: str
    description: str
    expectation: str  # plain English: what SHOULD happen

    headline: str = "Federal Reserve cuts rates by 25 basis points"
    pre_news_mid: float = 0.60
    spread: float = 0.02
    depth: float = 500.0

    # (seconds after the headline, new mid). Applied in order.
    price_path: list[tuple[float, float]] = field(default_factory=list)

    # Someone faster than us repriced the market BEFORE our feed delivered the
    # headline. This is the single most realistic failure mode for RSS-driven
    # trading, so it gets first-class support in the scenario language.
    jump_before_news: Optional[float] = None
    jump_lead_sec: float = 0.8

    warmup_sec: float = 4.0  # book history published BEFORE the headline
    runtime_sec: float = 14.0  # how long to let the engine run after it
    kill_after_sec: Optional[float] = None  # fire the kill switch mid-run

    # Per-scenario config surgery, e.g. a short time stop we can actually watch.
    strategy_patch: dict = field(default_factory=dict)

    # Assertions
    expect_position: bool = True
    expect_exit_tag: Optional[str] = None
    expect_reject_gate: Optional[str] = None


def _book(token_id: str, mid: float, spread: float, depth: float) -> OrderBook:
    """A simple symmetric two-sided book around `mid`."""
    half = spread / 2.0
    bid = round(max(0.01, mid - half), 2)
    ask = round(min(0.99, mid + half), 2)
    return OrderBook(
        token_id=token_id,
        bids=[Level(bid, depth), Level(round(bid - 0.01, 2), depth * 2)],
        asks=[Level(ask, depth), Level(round(ask + 0.01, 2), depth * 2)],
        ts_ms=now_ms(),
    )


class ScriptedFeed:
    """Stands in for MarketFeed. Publishes books on a timeline.

    Interface matches MarketFeed exactly (`start`, `stop`), because the engine
    must not know it is being simulated.
    """

    def __init__(self, store: BookStore, scenario: Scenario) -> None:
        self.store = store
        self.scenario = scenario
        self.current_mid = scenario.pre_news_mid
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.news_fired = asyncio.Event()

    def jump(self, mid: float) -> None:
        """Move the market now -- used to simulate a faster trader beating us."""
        log.info("[sim] market repriced to %.3f BEFORE our headline arrived", mid)
        self.current_mid = mid

    def publish(self, mid: float) -> None:
        self.store.set_book(_book(SIM_YES, mid, self.scenario.spread, self.scenario.depth))
        # The NO leg trades at 1 - mid. Needed for marking and for the hedge path.
        self.store.set_book(
            _book(SIM_NO, 1.0 - mid, self.scenario.spread, self.scenario.depth)
        )

    async def start(self, token_ids) -> None:  # noqa: ANN001 - matches MarketFeed
        self.store.connected = True
        self.publish(self.scenario.pre_news_mid)
        self._task = asyncio.create_task(self._run(), name="scripted-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.store.connected = False

    async def _run(self) -> None:
        """Keep the book fresh; walk the price path once the headline fires.

        Republishing every 250ms matters: the risk manager rejects books older
        than 1.5s, exactly as it would in production.
        """
        while not self.news_fired.is_set() and not self._stop.is_set():
            self.publish(self.current_mid)
            await asyncio.sleep(0.25)

        started = asyncio.get_running_loop().time()
        path = list(self.scenario.price_path)
        while not self._stop.is_set():
            elapsed = asyncio.get_running_loop().time() - started
            while path and path[0][0] <= elapsed:
                _, self.current_mid = path.pop(0)
                log.info("[sim] price path -> mid %.3f at t+%.1fs",
                         self.current_mid, elapsed)
            self.publish(self.current_mid)
            await asyncio.sleep(0.25)


class ScriptedNews:
    """Stands in for NewsMonitor. Emits one headline after a warmup delay.

    The warmup matters: the engine needs mid history from BEFORE the headline to
    compute its pre-news anchor. Firing news instantly would mean no anchor and
    an honest rejection -- which is itself worth seeing, and is scenario
    `no-anchor`.
    """

    def __init__(self, scenario: Scenario, feed: ScriptedFeed) -> None:
        self.scenario = scenario
        self.feed = feed
        self.events_seen = 0
        self.warmed_up = True
        self._task: Optional[asyncio.Task] = None

    async def prime(self) -> None:
        return None

    async def start(self, handler) -> None:  # noqa: ANN001 - matches NewsMonitor
        self._task = asyncio.create_task(self._fire(handler), name="scripted-news")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _fire(self, handler) -> None:  # noqa: ANN001
        scenario = self.scenario
        if scenario.jump_before_news is not None:
            # Publish the move first, THEN deliver the headline. The pre-news
            # anchor is taken from ~2s before delivery, so the engine sees an
            # anchor of 0.60 against a current price of 0.82 and must decline.
            await asyncio.sleep(max(0.0, scenario.warmup_sec - scenario.jump_lead_sec))
            self.feed.jump(scenario.jump_before_news)
            await asyncio.sleep(scenario.jump_lead_sec)
        else:
            await asyncio.sleep(scenario.warmup_sec)
        event = NewsEvent(
            event_id="sim-1",
            source="scripted",
            title=self.scenario.headline,
            summary="",
            url="https://example.invalid/sim",
            published_ms=now_ms(),
            seen_ms=now_ms(),
        )
        self.events_seen += 1
        log.info("[sim] HEADLINE: %s", event.title)
        self.feed.news_fired.set()
        await handler(event)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

SCENARIOS: list[Scenario] = [
    Scenario(
        name="clean-lag",
        description="Headline lands, market sits still for 3s, then reprices up.",
        expectation="ENTER on the lag, EXIT at take-profit, positive net P&L",
        price_path=[(3.0, 0.72), (5.0, 0.84)],
        expect_exit_tag="take_profit",
    ),
    Scenario(
        name="no-reprice",
        description="Headline lands but the market never moves. Our thesis was wrong.",
        expectation="ENTER, then EXIT on the time stop for a small loss",
        price_path=[],
        strategy_patch={"max_hold_sec": 6.0},
        runtime_sec=16.0,
        expect_exit_tag="time_stop",
    ),
    Scenario(
        name="adverse-move",
        description="Headline lands, then the market moves hard AGAINST us.",
        expectation="ENTER, then EXIT on the stop loss",
        price_path=[(2.0, 0.55), (3.5, 0.50)],
        expect_exit_tag="stop_loss",
    ),
    Scenario(
        name="already-repriced",
        description="The market moved BEFORE we could act. Someone was faster.",
        expectation="REJECT at the already_repriced gate -- never chase",
        jump_before_news=0.82,
        warmup_sec=5.0,
        runtime_sec=6.0,
        expect_position=False,
        expect_reject_gate="already_repriced",
    ),
    Scenario(
        name="thin-book",
        description="Correct signal, but only 10 shares are on the offer.",
        expectation="REJECT at the depth gate -- the fill would move the market",
        depth=10.0,
        runtime_sec=6.0,
        expect_position=False,
        expect_reject_gate="depth",
    ),
    Scenario(
        name="kill-switch",
        description="A position is open when the kill switch fires.",
        expectation="FLATTEN immediately and shut the engine down",
        price_path=[(3.0, 0.70)],
        kill_after_sec=6.0,
        runtime_sec=12.0,
        expect_exit_tag="flatten",
    ),
]


def scenario_by_name(name: str) -> Optional[Scenario]:
    return next((s for s in SCENARIOS if s.name == name), None)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #


@dataclass
class ScenarioResult:
    scenario: Scenario
    passed: bool
    detail: str
    entered: bool = False
    exit_tag: str = ""
    net_pnl: float = 0.0
    fees: float = 0.0
    equity: float = 0.0
    rejections: list[str] = field(default_factory=list)


def _sim_config(base: Config, scenario: Scenario, sim_dir: Path) -> Config:
    """Point the config at a throwaway directory and apply scenario patches.

    Critically this uses a SEPARATE state dir, so a simulation can never touch
    your real peak-equity record or trip your real kill switch.
    """
    market = MarketConfig(
        slug=SIM_MARKET_SLUG,
        note="simulated",
        triggers=[
            TriggerRule(
                name="cut-confirmed",
                outcome="YES",
                target_price=0.95,
                confidence=0.7,
                all_of=["federal reserve"],
                any_of=["cuts rates", "rate cut"],
                none_of=["expected to"],
            )
        ],
    )
    strategy = replace(base.strategy, **scenario.strategy_patch)
    execution = replace(base.execution, mode="paper", paper_latency_ms=200)
    # Wipe the scenario's directory first. Persisted peak equity and trade
    # counts are exactly right in production and exactly wrong here -- a
    # simulation must be reproducible, so every run starts from zero.
    import shutil

    scenario_dir = sim_dir / scenario.name
    if scenario_dir.exists():
        shutil.rmtree(scenario_dir, ignore_errors=True)
    state_dir = scenario_dir / "state"
    log_dir = scenario_dir / "logs"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    return replace(
        base,
        strategy=strategy,
        execution=execution,
        markets=[market],
        state_dir=state_dir,
        log_dir=log_dir,
    )


def _sim_market() -> MarketRef:
    return MarketRef(
        condition_id="0xsimulated",
        question="[SIMULATED] Will the Fed cut rates in September?",
        slug=SIM_MARKET_SLUG,
        yes_token_id=SIM_YES,
        no_token_id=SIM_NO,
        end_ts_ms=now_ms() + 30 * 24 * 3600 * 1000,
        min_tick=0.01,
        min_order_size=5.0,
    )


async def run_scenario(base: Config, scenario: Scenario, sim_dir: Path) -> ScenarioResult:
    cfg = _sim_config(base, scenario, sim_dir)
    market = _sim_market()

    feed_holder: dict[str, ScriptedFeed] = {}

    def make_feed(store: BookStore) -> ScriptedFeed:
        feed = ScriptedFeed(store, scenario)
        feed_holder["feed"] = feed
        return feed

    engine = Engine(
        cfg, "paper",
        overrides=Overrides(
            markets={SIM_MARKET_SLUG: market},
            feed_factory=make_feed,
            skip_health_check=True,
            install_signal_handlers=False,
        ),
    )
    engine.news = ScriptedNews(scenario, None)  # type: ignore[arg-type]

    task = asyncio.create_task(engine.run(), name=f"sim-{scenario.name}")

    # The feed object only exists once setup() runs; wire news to it after.
    for _ in range(100):
        if "feed" in feed_holder:
            engine.news.feed = feed_holder["feed"]  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.05)

    if scenario.kill_after_sec is not None:
        async def fire_kill() -> None:
            await asyncio.sleep(scenario.kill_after_sec)
            if engine.risk:
                log.warning("[sim] firing the kill switch")
                engine.risk.kill("simulated operator kill")
        asyncio.create_task(fire_kill(), name="sim-kill")

    deadline = scenario.warmup_sec + scenario.runtime_sec
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=deadline)
    except asyncio.TimeoutError:
        await engine.shutdown("scenario complete")
        await task
    except Exception:  # noqa: BLE001
        log.exception("[sim] scenario %s raised", scenario.name)

    return _grade(engine, scenario)


def _grade(engine: Engine, scenario: Scenario) -> ScenarioResult:
    portfolio = engine.portfolio
    assert portfolio is not None

    trades = portfolio.closed_trades
    entered = bool(trades) or bool(portfolio.open_positions())
    exit_tag = trades[-1].exit_reason if trades else ""
    net = sum(t.net_pnl for t in trades)

    result = ScenarioResult(
        scenario=scenario,
        passed=False,
        detail="",
        entered=entered,
        exit_tag=exit_tag,
        net_pnl=net,
        fees=portfolio.total_fees,
        equity=portfolio.equity(engine._books_for_marking()),  # noqa: SLF001
    )

    if scenario.expect_position and not entered:
        result.detail = "expected an entry, got none"
        return result
    if not scenario.expect_position and entered:
        result.detail = "expected NO entry, but a position was opened"
        return result

    if scenario.expect_exit_tag:
        # Exit reasons are free text; the tag is the stable part.
        if not trades:
            result.detail = f"expected a {scenario.expect_exit_tag} exit, no trade closed"
            return result
        matched = _tag_matches(scenario.expect_exit_tag, trades[-1].exit_reason)
        if not matched:
            result.detail = (
                f"expected exit '{scenario.expect_exit_tag}', got '{trades[-1].exit_reason}'"
            )
            return result

    if scenario.expect_reject_gate:
        gates = _rejection_gates(engine)
        result.rejections = gates
        if scenario.expect_reject_gate not in gates:
            result.detail = (
                f"expected a '{scenario.expect_reject_gate}' rejection, saw {gates or 'none'}"
            )
            return result

    result.passed = True
    result.detail = "as expected"
    return result


_TAG_HINTS = {
    "take_profit": ("take profit",),
    "stop_loss": ("stop loss",),
    "time_stop": ("time stop",),
    "flatten": ("kill switch", "forced flatten", "shutdown"),
    "resolution_guard": ("resolution",),
}


def _tag_matches(tag: str, reason: str) -> bool:
    reason = reason.lower()
    return any(hint in reason for hint in _TAG_HINTS.get(tag, (tag,)))


def _rejection_gates(engine: Engine) -> list[str]:
    """Pull the [gate] markers out of this run's decisions.csv."""
    import csv

    path = Path(engine.cfg.log_dir) / "decisions.csv"
    if not path.exists():
        return []
    gates: list[str] = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("action") != "REJECT":
                continue
            reason = row.get("reason", "")
            if reason.startswith("["):
                gates.append(reason[1: reason.index("]")] if "]" in reason else reason)
    return gates


async def run_all(
    base: Config, sim_dir: Path, only: Optional[str] = None
) -> list[ScenarioResult]:
    chosen = SCENARIOS if only is None else [s for s in SCENARIOS if s.name == only]
    if not chosen:
        raise ValueError(f"unknown scenario {only!r}")

    results: list[ScenarioResult] = []
    for scenario in chosen:
        print(f"\n{'=' * 70}")
        print(f"SCENARIO  {scenario.name}")
        print(f"  story   {scenario.description}")
        print(f"  expect  {scenario.expectation}")
        print("=" * 70)
        results.append(await run_scenario(base, scenario, sim_dir))
    return results


def format_results(results: list[ScenarioResult]) -> str:
    lines = ["", "=" * 70, "SIMULATION SUMMARY", "=" * 70]
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        lines.append(f"[{mark}] {r.scenario.name:<18} {r.detail}")
        if r.entered:
            lines.append(
                f"         entered=yes exit={r.exit_tag or 'still open'} "
                f"net={r.net_pnl:+.4f} fees={r.fees:.4f} equity={r.equity:.2f}"
            )
        elif r.rejections:
            lines.append(f"         rejected at: {', '.join(sorted(set(r.rejections)))}")
    passed = sum(1 for r in results if r.passed)
    lines += [
        "-" * 70,
        f"{passed}/{len(results)} scenarios behaved as designed.",
        "",
        "This proves the MACHINERY works end to end: news -> match -> fair value",
        "-> gates -> risk -> sizing -> fill -> accounting -> exit -> journal.",
        "",
        "It proves NOTHING about profitability. The price paths above were",
        "written by hand to exercise each code path, not sampled from real",
        "market behaviour. Only `watch` and `paper` on live markets can tell you",
        "whether a lag actually exists for you to trade.",
        "=" * 70,
    ]
    return "\n".join(lines)

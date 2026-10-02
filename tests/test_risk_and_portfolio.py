"""Risk limits, sizing, kill switch, portfolio accounting and paper fills.

If any test in this file starts failing, stop trading until it passes again.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from polylag.execution.base import taker_fee
from polylag.execution.paper import PaperBroker
from polylag.journal import StateStore
from polylag.models import Fill, OrderBook, OrderIntent, Level, Signal, now_ms
from polylag.portfolio import Portfolio
from polylag.risk.manager import RiskManager


@pytest.fixture
def signal(market):
    return Signal(
        market=market, token_id=market.yes_token_id, outcome="YES", side="BUY",
        reference_price=0.62, fair_value=0.85, edge=0.21, trigger_name="t",
        news_event_id="e", news_title="Fed cuts rates", rationale="r",
    )


@pytest.fixture
def risk(cfg, tmp_path):
    portfolio = Portfolio(cfg.risk.starting_bankroll_usdc)
    return RiskManager(cfg, StateStore(tmp_path / "state"), portfolio), portfolio


# -- sizing ------------------------------------------------------------------ #


def test_sizing_takes_the_smallest_cap(risk, signal, market, book, cfg):
    manager, portfolio = risk
    sized, verdict = manager.size_order(signal, market, book, equity=800.0)
    assert sized is not None, verdict.reason
    # 1% of 800 = $8.00 is the binding cap here, below the $10 per-trade cap.
    assert sized.notional <= 8.0 + 1e-9
    assert sized.binding_constraint == "pct_of_equity"


def test_sizing_respects_depth_participation(risk, signal, market, cfg):
    manager, _ = risk
    thin = OrderBook(
        "TOK_YES", bids=[Level(0.60, 500)], asks=[Level(0.62, 20)], ts_ms=now_ms()
    )
    sized, _ = manager.size_order(signal, market, thin, equity=100_000.0)
    # 25% of (20 + 0 within limit) = 5 shares max
    assert sized is None or sized.shares <= 5


def test_sizing_refuses_below_venue_minimum(risk, signal, market, book):
    manager, _ = risk
    sized, verdict = manager.size_order(signal, market, book, equity=20.0)
    assert sized is None
    assert verdict.gate == "below_min_size"


# -- gates ------------------------------------------------------------------- #


def test_blocks_when_feed_is_disconnected(risk, signal, book):
    manager, _ = risk
    verdict = manager.can_open(signal, book, 600.0, feed_connected=False, news_age_sec=5)
    assert not verdict.ok and verdict.gate == "feed_health"


def test_blocks_stale_news(risk, signal, book):
    manager, _ = risk
    verdict = manager.can_open(signal, book, 600.0, feed_connected=True, news_age_sec=500)
    assert not verdict.ok and verdict.gate == "stale_news"


def test_blocks_stale_book(risk, signal):
    manager, _ = risk
    stale = OrderBook(
        "TOK_YES", bids=[Level(0.60, 5)], asks=[Level(0.62, 5)], ts_ms=now_ms() - 60_000
    )
    verdict = manager.can_open(signal, stale, 600.0, True, 5)
    assert not verdict.ok and verdict.gate == "stale_book"


def test_blocks_duplicate_and_opposite_legs(risk, signal, book, market):
    manager, portfolio = risk
    fill = Fill("o1", market.yes_token_id, "BUY", 10, 0.62, 0.0)
    portfolio.apply_buy(market, fill, signal)

    verdict = manager.can_open(signal, book, 600.0, True, 5)
    assert not verdict.ok and verdict.gate == "already_long"

    no_signal = replace(signal, token_id=market.no_token_id, outcome="NO")
    portfolio.positions.clear()
    portfolio.apply_buy(market, fill, signal)
    verdict = manager.can_open(no_signal, book, 600.0, True, 5)
    assert not verdict.ok and verdict.gate == "opposite_leg"


def test_hourly_throttle(risk, signal, book, cfg):
    manager, _ = risk
    for _ in range(cfg.risk.max_new_trades_per_hour):
        manager.record_entry()
    manager._last_trade_ms = 0  # bypass the separate cooldown gate
    verdict = manager.can_open(signal, book, 600.0, True, 5)
    assert not verdict.ok and verdict.gate == "hourly_throttle"


def test_cooldown_after_a_trade(risk, signal, book):
    manager, _ = risk
    manager.record_entry()
    verdict = manager.can_open(signal, book, 600.0, True, 5)
    assert not verdict.ok and verdict.gate == "cooldown"


def test_loss_streak_pauses_entries(risk, signal, book, cfg):
    manager, _ = risk
    for _ in range(cfg.risk.loss_streak_pause):
        manager.record_close(-1.0)
    manager._last_trade_ms = 0
    verdict = manager.can_open(signal, book, 600.0, True, 5)
    assert not verdict.ok and verdict.gate == "loss_streak"


def test_a_win_resets_the_loss_streak(risk):
    manager, _ = risk
    manager.record_close(-1.0)
    manager.record_close(-1.0)
    manager.record_close(+1.0)
    assert manager._consecutive_losses == 0


# -- escalation -------------------------------------------------------------- #


def test_daily_loss_halts_but_does_not_latch(risk, cfg):
    manager, _ = risk
    manager.day_start_equity = 600.0
    manager.peak_equity = 600.0
    verdict = manager.update_equity(585.0)  # -2.5%, limit is 2%
    assert not verdict.ok and verdict.gate == "daily_loss"
    assert not manager.is_killed()  # a day halt must NOT be permanent


def test_max_drawdown_latches_the_kill_switch(risk, cfg):
    manager, _ = risk
    manager.peak_equity = 600.0
    manager.day_start_equity = 600.0
    verdict = manager.update_equity(540.0)  # -10% from peak, limit is 6%
    assert not verdict.ok
    assert manager.is_killed()
    assert "drawdown" in manager.state.kill_reason()


def test_equity_floor_latches_the_kill_switch(risk, cfg):
    manager, _ = risk
    manager.update_equity(399.0)  # floor is 400
    assert manager.is_killed()
    assert "floor" in manager.state.kill_reason()


def test_kill_latch_survives_a_new_manager(cfg, tmp_path):
    state = StateStore(tmp_path / "state")
    portfolio = Portfolio(600.0)
    first = RiskManager(cfg, state, portfolio)
    first.kill("test")

    reborn = RiskManager(cfg, StateStore(tmp_path / "state"), Portfolio(600.0))
    assert reborn.is_killed()


def test_peak_equity_persists_across_restarts(cfg, tmp_path):
    portfolio = Portfolio(600.0)
    first = RiskManager(cfg, StateStore(tmp_path / "state"), portfolio)
    first.update_equity(800.0)

    reborn = RiskManager(cfg, StateStore(tmp_path / "state"), Portfolio(600.0))
    assert reborn.peak_equity == 800.0
    # A restart must not hand us a fresh drawdown allowance: 740 is -7.5% from
    # the remembered peak and must latch the kill switch immediately.
    assert not reborn.update_equity(740.0).ok
    assert reborn.is_killed()


def test_killed_manager_blocks_every_entry(risk, signal, book):
    manager, _ = risk
    manager.kill("manual")
    verdict = manager.can_open(signal, book, 600.0, True, 5)
    assert not verdict.ok and verdict.gate == "kill_switch"


# -- portfolio --------------------------------------------------------------- #


def test_buy_then_sell_accounting(market, signal):
    portfolio = Portfolio(100.0)
    portfolio.apply_buy(market, Fill("o1", "TOK_YES", "BUY", 50, 0.60, 0.10), signal)
    assert abs(portfolio.cash - (100 - 30 - 0.10)) < 1e-9

    trade = portfolio.apply_sell(Fill("o2", "TOK_YES", "SELL", 50, 0.70, 0.10), "tp")
    assert trade is not None
    assert abs(trade.gross_pnl - 5.0) < 1e-9
    assert abs(trade.net_pnl - (5.0 - 0.20)) < 1e-9  # both fees subtracted
    assert portfolio.open_positions() == []


def test_average_price_on_a_second_buy(market, signal):
    portfolio = Portfolio(100.0)
    portfolio.apply_buy(market, Fill("o1", "TOK_YES", "BUY", 10, 0.50, 0.0), signal)
    portfolio.apply_buy(market, Fill("o2", "TOK_YES", "BUY", 10, 0.60, 0.0), signal)
    assert abs(portfolio.positions["TOK_YES"].avg_price - 0.55) < 1e-9


def test_equity_marks_at_the_bid_not_the_mid(market, signal, book):
    portfolio = Portfolio(100.0)
    portfolio.apply_buy(market, Fill("o1", "TOK_YES", "BUY", 100, 0.62, 0.0), signal)
    equity = portfolio.equity({"TOK_YES": book})
    # cash 38 + 100 shares marked at the 0.60 BID = 98, not at the 0.61 mid.
    assert abs(equity - 98.0) < 1e-9


def test_fee_formula_uses_the_cheaper_side():
    # 100 shares at 0.97 -> charged on min(0.97, 0.03) = 0.03
    assert abs(taker_fee(100, 0.97, 100) - (0.01 * 0.03 * 100)) < 1e-12
    assert taker_fee(100, 0.5, 0) == 0.0


# -- paper broker ------------------------------------------------------------ #


class _Store:
    def __init__(self, book):
        self._book = book

    def get(self, token_id):
        return self._book


def test_paper_fill_applies_participation_and_slippage(cfg, book, market):
    execution = replace(cfg.execution, paper_latency_ms=0, paper_adverse_ticks=1,
                        paper_fill_participation=0.5)
    broker = PaperBroker(execution, _Store(book), tick=0.01)
    intent = OrderIntent(market, "TOK_YES", "BUY", shares=1000, limit_price=0.62,
                         reason="test")
    result = asyncio.run(broker.buy(intent))

    assert result.status == "partial"
    assert result.fill is not None
    # limit 0.62 + 1 tick = 0.63; half of (400 + 900) = 650 shares available.
    assert abs(result.fill.shares - 650) < 1e-6


def test_paper_rejects_when_price_is_outside_the_limit(cfg, market):
    execution = replace(cfg.execution, paper_latency_ms=0)
    far = OrderBook("TOK_YES", bids=[Level(0.30, 500)], asks=[Level(0.90, 500)],
                    ts_ms=now_ms())
    broker = PaperBroker(execution, _Store(far), tick=0.01)
    intent = OrderIntent(market, "TOK_YES", "BUY", 10, 0.62, "test")
    result = asyncio.run(broker.buy(intent))
    assert result.status == "rejected"


def test_paper_refuses_a_crossed_book(cfg, market):
    execution = replace(cfg.execution, paper_latency_ms=0)
    crossed = OrderBook("TOK_YES", bids=[Level(0.70, 10)], asks=[Level(0.65, 10)],
                        ts_ms=now_ms())
    broker = PaperBroker(execution, _Store(crossed), tick=0.01)
    intent = OrderIntent(market, "TOK_YES", "BUY", 10, 0.70, "test")
    assert asyncio.run(broker.buy(intent)).status == "rejected"

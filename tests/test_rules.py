"""Entry and exit rules. Each gate gets a test that proves it can say no."""

from __future__ import annotations

import pytest

from polylag.models import Level, OrderBook, Position, now_ms
from polylag.news.matcher import TriggerMatch
from polylag.strategy import rules


@pytest.fixture
def match(market, trigger):
    from polylag.config import MarketConfig

    return TriggerMatch(
        market=MarketConfig(slug=market.slug, triggers=[trigger]),
        trigger=trigger,
        matched_terms=["federal reserve", "cuts rates"],
    )


def evaluate(match, news, market, book, cfg, pre_news_mid=0.61):
    return rules.evaluate_entry(
        match=match, event=news, market=market, book=book,
        pre_news_mid=pre_news_mid, cfg=cfg.strategy,
        max_book_staleness_ms=cfg.risk.max_book_staleness_ms,
        max_spread=cfg.risk.max_spread,
        min_depth_shares=cfg.risk.min_top_of_book_depth_shares,
    )


def test_accepts_a_clean_lagged_setup(match, news, market, book, cfg):
    result = evaluate(match, news, market, book, cfg)
    assert result.accepted, result.reason
    signal = result.signal
    assert signal is not None
    assert signal.side == "BUY" and signal.outcome == "YES"
    assert signal.token_id == market.yes_token_id
    # fair 0.848, ask 0.62, buffer 0.02 -> edge 0.208
    assert abs(signal.edge - 0.208) < 1e-6


def test_rejects_without_a_pre_news_anchor(match, news, market, book, cfg):
    result = evaluate(match, news, market, book, cfg, pre_news_mid=None)
    assert not result.accepted
    assert result.gate == "pre_news_anchor"


def test_rejects_when_the_market_already_moved(match, news, market, cfg):
    moved = OrderBook(
        "TOK_YES", bids=[Level(0.80, 500)], asks=[Level(0.82, 500)], ts_ms=now_ms()
    )
    result = evaluate(match, news, market, moved, cfg, pre_news_mid=0.61)
    assert not result.accepted
    assert result.gate == "already_repriced"


def test_rejects_a_stale_book(match, news, market, cfg):
    stale = OrderBook(
        "TOK_YES", bids=[Level(0.60, 500)], asks=[Level(0.62, 500)],
        ts_ms=now_ms() - 10_000,
    )
    result = evaluate(match, news, market, stale, cfg)
    assert not result.accepted and result.gate == "book_fresh"


def test_rejects_a_wide_spread(match, news, market, cfg):
    wide = OrderBook(
        "TOK_YES", bids=[Level(0.50, 500)], asks=[Level(0.62, 500)], ts_ms=now_ms()
    )
    result = evaluate(match, news, market, wide, cfg, pre_news_mid=0.56)
    assert not result.accepted and result.gate == "spread"


def test_rejects_thin_top_of_book(match, news, market, cfg):
    thin = OrderBook(
        "TOK_YES", bids=[Level(0.60, 500)], asks=[Level(0.62, 5)], ts_ms=now_ms()
    )
    result = evaluate(match, news, market, thin, cfg)
    assert not result.accepted and result.gate == "depth"


def test_rejects_insufficient_edge(match, news, market, book, cfg):
    # Anchor already near the target leaves nothing to capture.
    result = evaluate(match, news, market, book, cfg, pre_news_mid=0.61)
    assert result.accepted  # sanity: the fixture setup does have edge

    from polylag.config import TriggerRule

    weak = TriggerRule(
        name="weak", outcome="YES", target_price=0.63, confidence=0.2, any_of=["a"]
    )
    weak_match = TriggerMatch(match.market, weak, ["a"])
    result = evaluate(weak_match, news, market, book, cfg)
    assert not result.accepted and result.gate == "edge"


def test_rejects_when_resolution_is_imminent(match, news, market, book, cfg):
    from dataclasses import replace

    soon = replace(market, end_ts_ms=now_ms() + 60_000)
    result = evaluate(match, news, soon, book, cfg)
    assert not result.accepted and result.gate == "time_to_resolution"


def test_rejects_a_one_sided_book(match, news, market, cfg):
    one_sided = OrderBook("TOK_YES", bids=[], asks=[Level(0.62, 500)], ts_ms=now_ms())
    result = evaluate(match, news, market, one_sided, cfg)
    assert not result.accepted and result.gate == "book_two_sided"


# -- exits ------------------------------------------------------------------- #


def make_position(market, entry=0.62, fair=0.85, age_sec=0.0):
    from polylag.models import Signal

    signal = Signal(
        market=market, token_id=market.yes_token_id, outcome="YES", side="BUY",
        reference_price=entry, fair_value=fair, edge=0.2, trigger_name="t",
        news_event_id="e", news_title="n", rationale="r",
    )
    return Position(
        market=market, token_id=market.yes_token_id, outcome="YES",
        shares=100, avg_price=entry, entry_signal=signal,
        opened_ms=now_ms() - int(age_sec * 1000),
    )


def test_stop_loss_fires_on_adverse_move(market, cfg):
    position = make_position(market)
    book = OrderBook("TOK_YES", bids=[Level(0.56, 500)], asks=[Level(0.58, 500)])
    decision = rules.evaluate_exit(position, book, cfg.strategy)
    assert decision.should_exit and decision.tag == "stop_loss"


def test_take_profit_fires_at_the_capture_threshold(market, cfg):
    position = make_position(market, entry=0.62, fair=0.85)
    # target move = (0.85 - 0.62) * 0.6 = 0.138 -> bid must reach ~0.758
    book = OrderBook("TOK_YES", bids=[Level(0.76, 500)], asks=[Level(0.78, 500)])
    decision = rules.evaluate_exit(position, book, cfg.strategy)
    assert decision.should_exit and decision.tag == "take_profit"


def test_time_stop_fires(market, cfg):
    position = make_position(market, age_sec=cfg.strategy.max_hold_sec + 1)
    book = OrderBook("TOK_YES", bids=[Level(0.62, 500)], asks=[Level(0.63, 500)])
    decision = rules.evaluate_exit(position, book, cfg.strategy)
    assert decision.should_exit and decision.tag == "time_stop"


def test_resolution_guard_outranks_everything(market, cfg):
    from dataclasses import replace

    position = make_position(market)
    position.market = replace(market, end_ts_ms=now_ms() + 60_000)
    book = OrderBook("TOK_YES", bids=[Level(0.90, 500)], asks=[Level(0.92, 500)])
    decision = rules.evaluate_exit(position, book, cfg.strategy)
    assert decision.should_exit and decision.tag == "resolution_guard"


def test_no_exit_when_nothing_has_happened(market, cfg):
    position = make_position(market)
    book = OrderBook("TOK_YES", bids=[Level(0.62, 500)], asks=[Level(0.63, 500)])
    assert not rules.evaluate_exit(position, book, cfg.strategy).should_exit


def test_missing_bid_does_not_pretend_to_exit(market, cfg):
    position = make_position(market)
    book = OrderBook("TOK_YES", bids=[], asks=[Level(0.63, 500)])
    decision = rules.evaluate_exit(position, book, cfg.strategy)
    assert not decision.should_exit
    assert "no bid" in decision.reason

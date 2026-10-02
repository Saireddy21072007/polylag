"""Order-book maths, headline matching, and the fair-value formula."""

from __future__ import annotations

from polylag.config import TriggerRule
from polylag.models import Level, OrderBook, now_ms
from polylag.news.matcher import TriggerMatcher, contains_phrase, normalize
from polylag.strategy import fair_value as fv


# -- book -------------------------------------------------------------------- #


def test_book_basics(book):
    assert book.best_bid == 0.60
    assert book.best_ask == 0.62
    assert abs(book.mid - 0.61) < 1e-9
    assert abs(book.spread - 0.02) < 1e-9
    assert not book.is_crossed()


def test_crossed_book_is_detected():
    bad = OrderBook("T", bids=[Level(0.70, 10)], asks=[Level(0.65, 10)])
    assert bad.is_crossed()


def test_walk_stops_at_limit_price(book):
    # Only the 0.62 level (400) is at or better than a 0.62 limit.
    filled, avg = book.walk("BUY", 1000, 0.62)
    assert filled == 400
    assert avg == 0.62


def test_walk_averages_across_levels(book):
    filled, avg = book.walk("BUY", 1000, 0.63)
    assert filled == 1000
    expected = (400 * 0.62 + 600 * 0.63) / 1000
    assert abs(avg - expected) < 1e-9


def test_walk_returns_zero_when_nothing_is_within_limit(book):
    filled, avg = book.walk("BUY", 100, 0.50)
    assert filled == 0 and avg == 0.0


def test_depth_up_to(book):
    assert book.depth_up_to("BUY", 0.63) == 1300
    assert book.depth_up_to("SELL", 0.59) == 1300


# -- matching ---------------------------------------------------------------- #


def test_normalize_strips_punctuation_and_case():
    assert normalize("Fed CUTS rates, again!") == " fed cuts rates again "


def test_phrase_matching_respects_word_boundaries():
    text = normalize("the haircut was expensive")
    assert not contains_phrase(text, "cut")
    assert contains_phrase(normalize("Fed cut rates"), "cut")


def test_matcher_requires_all_of(market, trigger, news):
    from polylag.config import MarketConfig
    from polylag.models import NewsEvent

    matcher = TriggerMatcher([MarketConfig(slug=market.slug, triggers=[trigger])])
    assert len(matcher.match(news)) == 1

    off_topic = NewsEvent("e2", "test", "Bank of England cuts rates", "", "", None)
    assert matcher.match(off_topic) == []


def test_none_of_vetoes(market, trigger):
    from polylag.config import MarketConfig
    from polylag.models import NewsEvent

    matcher = TriggerMatcher([MarketConfig(slug=market.slug, triggers=[trigger])])
    hedged = NewsEvent(
        "e3", "test", "Federal Reserve expected to cut rates next month", "", "", None
    )
    assert matcher.match(hedged) == []


# -- fair value -------------------------------------------------------------- #


def test_fair_value_blends_anchor_toward_target(trigger):
    # 0.61 + 0.7 * (0.95 - 0.61) = 0.848
    assert abs(fv.estimate_fair_value(0.61, trigger) - 0.848) < 1e-9


def test_fair_value_is_clamped():
    aggressive = TriggerRule(
        name="x", outcome="YES", target_price=0.99, confidence=1.0, any_of=["a"]
    )
    assert fv.estimate_fair_value(0.995, aggressive) <= 0.99


def test_confidence_zero_would_never_trade(trigger):
    calm = TriggerRule(
        name="x", outcome="YES", target_price=0.95, confidence=0.01, any_of=["a"]
    )
    assert abs(fv.estimate_fair_value(0.61, calm) - 0.6134) < 1e-6


def test_edge_subtracts_the_cost_buffer():
    assert abs(fv.edge_for_buy(0.85, 0.62, 0.02) - 0.21) < 1e-9
    assert fv.edge_for_buy(0.85, None, 0.02) is None

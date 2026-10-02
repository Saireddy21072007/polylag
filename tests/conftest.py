"""Shared fixtures. No network, no keys, no disk outside tmp_path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polylag.config import (  # noqa: E402
    Config, EndpointsConfig, ExecutionConfig, FeedConfig, MarketConfig,
    NewsConfig, RiskConfig, StrategyConfig, TriggerRule,
)
from polylag.models import Level, MarketRef, NewsEvent, OrderBook, now_ms  # noqa: E402


@pytest.fixture
def market() -> MarketRef:
    return MarketRef(
        condition_id="0xcond",
        question="Will the Fed cut rates in September?",
        slug="fed-cut-september",
        yes_token_id="TOK_YES",
        no_token_id="TOK_NO",
        end_ts_ms=now_ms() + 7 * 24 * 3600 * 1000,
        min_tick=0.01,
        min_order_size=5.0,
    )


@pytest.fixture
def book() -> OrderBook:
    """A healthy two-sided book: 0.60 bid / 0.62 ask, decent depth."""
    return OrderBook(
        token_id="TOK_YES",
        bids=[Level(0.60, 500), Level(0.59, 800), Level(0.58, 1200)],
        asks=[Level(0.62, 400), Level(0.63, 900), Level(0.64, 1500)],
        ts_ms=now_ms(),
    )


@pytest.fixture
def trigger() -> TriggerRule:
    return TriggerRule(
        name="cut-confirmed",
        outcome="YES",
        target_price=0.95,
        confidence=0.7,
        all_of=["federal reserve"],
        any_of=["cuts rates", "rate cut"],
        none_of=["expected to", "could"],
    )


@pytest.fixture
def news() -> NewsEvent:
    return NewsEvent(
        event_id="evt1",
        source="test",
        title="Federal Reserve cuts rates by 25 basis points",
        summary="",
        url="https://example.test/1",
        published_ms=now_ms(),
        seen_ms=now_ms(),
    )


@pytest.fixture
def cfg(tmp_path, trigger, market) -> Config:
    return Config(
        risk=RiskConfig(),
        strategy=StrategyConfig(),
        execution=ExecutionConfig(),
        endpoints=EndpointsConfig(),
        news=NewsConfig(feeds=[FeedConfig(name="t", url="https://example.test/rss")]),
        markets=[MarketConfig(slug=market.slug, triggers=[trigger])],
        state_dir=tmp_path / "state",
        log_dir=tmp_path / "logs",
    )

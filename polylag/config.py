"""Typed configuration loader.

Every tunable lives in config.yaml; every secret lives in .env. Nothing is
hardcoded in the strategy modules, so you can change risk without touching code
and can diff two config files to explain two different result sets.

The loader is deliberately strict: unknown keys and out-of-range risk numbers
raise at startup rather than at 3am with real money on the line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised for anything wrong in config.yaml or the environment."""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskConfig:
    """Hard limits. These are checked before EVERY order, no exceptions.

    All percentages are of *current* equity unless noted.
    """

    starting_bankroll_usdc: float = 600.0  # see config.yaml: must clear the venue minimum

    # Position sizing
    max_trade_pct_of_equity: float = 1.0  # % of equity risked per single entry
    max_notional_per_trade_usdc: float = 10.0  # absolute cap, whichever binds first
    max_notional_per_market_usdc: float = 20.0  # total across all legs in one market
    max_gross_exposure_pct: float = 10.0  # sum of all open cost bases
    max_open_positions: int = 3

    # Throttles
    max_new_trades_per_hour: int = 6
    max_new_trades_per_day: int = 20
    cooldown_after_trade_sec: int = 60
    loss_streak_pause: int = 3  # consecutive losers before a pause
    loss_streak_cooldown_sec: int = 3600

    # Loss limits (breaching these stops trading)
    daily_loss_limit_pct: float = 2.0  # of the day's starting equity -> halt for day
    max_drawdown_pct: float = 6.0  # from peak equity -> permanent kill latch
    min_equity_floor_usdc: float = 400.0  # below this -> permanent kill latch

    # Data-quality gates (a stale book is how you donate money)
    max_book_staleness_ms: int = 1500
    max_news_age_sec: float = 90.0
    max_spread: float = 0.04
    min_top_of_book_depth_shares: float = 50.0
    max_depth_participation: float = 0.25  # take at most 25% of visible depth

    def validate(self) -> None:
        if not 0 < self.max_trade_pct_of_equity <= 5:
            raise ConfigError("risk.max_trade_pct_of_equity must be in (0, 5]")
        if not 0 < self.daily_loss_limit_pct <= 10:
            raise ConfigError("risk.daily_loss_limit_pct must be in (0, 10]")
        if not 0 < self.max_drawdown_pct <= 25:
            raise ConfigError("risk.max_drawdown_pct must be in (0, 25]")
        if self.max_drawdown_pct <= self.daily_loss_limit_pct:
            raise ConfigError(
                "risk.max_drawdown_pct must exceed daily_loss_limit_pct, "
                "otherwise the daily halt can never trigger before the kill switch"
            )
        if self.max_open_positions < 1:
            raise ConfigError("risk.max_open_positions must be >= 1")
        if not 0 < self.max_depth_participation <= 1:
            raise ConfigError("risk.max_depth_participation must be in (0, 1]")
        if self.min_equity_floor_usdc >= self.starting_bankroll_usdc:
            raise ConfigError(
                "risk.min_equity_floor_usdc must be below starting_bankroll_usdc"
            )


@dataclass(frozen=True)
class StrategyConfig:
    """Entry/exit thresholds. All in probability units (0.01 = one cent)."""

    min_edge: float = 0.06  # required fair_value - ask, BEFORE cost buffer
    cost_buffer: float = 0.02  # haircut for fees, slippage, being wrong
    min_entry_price: float = 0.08  # avoid lottery tickets
    max_entry_price: float = 0.90  # avoid pennies-in-front-of-steamroller
    max_price_move_since_news: float = 0.03  # if it already moved, the lag is gone
    quiet_window_sec: float = 120.0  # window used for the pre-news reference price

    take_profit_edge_capture: float = 0.6  # exit once 60% of the edge is realised
    stop_loss: float = 0.05  # adverse move (in probability) that closes us out
    max_hold_sec: float = 900.0  # time stop
    min_seconds_to_resolution: float = 1800.0  # never carry into settlement
    exit_slippage_allowance: float = 0.03  # how far we cross to get out

    def validate(self) -> None:
        if self.min_edge <= self.cost_buffer:
            raise ConfigError("strategy.min_edge must exceed strategy.cost_buffer")
        if not 0 < self.min_entry_price < self.max_entry_price < 1:
            raise ConfigError("strategy entry price bounds must satisfy 0 < min < max < 1")
        if not 0 < self.take_profit_edge_capture <= 1:
            raise ConfigError("strategy.take_profit_edge_capture must be in (0, 1]")


@dataclass(frozen=True)
class ExecutionConfig:
    mode: str = "paper"  # paper | live  (CLI flags can only make this safer)
    fee_bps: float = 0.0  # taker fee in basis points; VERIFY against the API
    paper_latency_ms: int = 400  # assumed round trip before our order lands
    paper_adverse_ticks: int = 1  # extra ticks of slippage assumed on every fill
    paper_fill_participation: float = 0.5  # only half of displayed size is real
    order_type: str = "FAK"  # fill-and-kill: never rest a stale opinion on the book
    max_order_retries: int = 2

    def validate(self) -> None:
        if self.mode not in ("paper", "live"):
            raise ConfigError("execution.mode must be 'paper' or 'live'")
        if self.order_type not in ("FAK", "FOK", "GTC"):
            raise ConfigError("execution.order_type must be FAK, FOK or GTC")
        if not 0 < self.paper_fill_participation <= 1:
            raise ConfigError("execution.paper_fill_participation must be in (0, 1]")


@dataclass(frozen=True)
class EndpointsConfig:
    gamma: str = "https://gamma-api.polymarket.com"
    clob: str = "https://clob.polymarket.com"
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    chain_id: int = 137  # Polygon mainnet
    http_timeout_sec: float = 10.0
    rate_limit_per_sec: float = 5.0  # our self-imposed ceiling, well under theirs
    rate_limit_burst: int = 10
    # Set this if your network blocks Polymarket (symptom: TLS connection reset
    # at handshake). Applies to HTTP; the websocket also tries to use it, and
    # falls back to a direct connection when the library cannot.
    proxy_url: Optional[str] = None


@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    poll_sec: float = 20.0
    enabled: bool = True


@dataclass(frozen=True)
class NewsConfig:
    feeds: list[FeedConfig] = field(default_factory=list)
    max_headlines_per_poll: int = 40
    dedupe_cache_size: int = 5000
    ignore_older_than_sec: float = 300.0  # a headline older than this is not "breaking"


@dataclass(frozen=True)
class TriggerRule:
    """A human-written, auditable mapping from headline text to a price target.

    There is no model here on purpose. If you cannot write the rule down in one
    line of English, you do not have an edge -- you have a hunch.
    """

    name: str
    outcome: str  # YES | NO -- which token this headline favours
    target_price: float  # where you believe the market should trade
    confidence: float = 0.5  # 0..1, how far to move from the pre-news price
    all_of: list[str] = field(default_factory=list)  # every phrase must appear
    any_of: list[str] = field(default_factory=list)  # at least one must appear
    none_of: list[str] = field(default_factory=list)  # veto phrases

    def validate(self, market_slug: str) -> None:
        where = f"markets[{market_slug}].triggers[{self.name}]"
        if self.outcome not in ("YES", "NO"):
            raise ConfigError(f"{where}.outcome must be YES or NO")
        if not 0 < self.target_price < 1:
            raise ConfigError(f"{where}.target_price must be in (0, 1)")
        if not 0 < self.confidence <= 1:
            raise ConfigError(f"{where}.confidence must be in (0, 1]")
        if not self.all_of and not self.any_of:
            raise ConfigError(f"{where} needs at least one all_of/any_of phrase")


@dataclass(frozen=True)
class MarketConfig:
    slug: str
    enabled: bool = True
    note: str = ""
    triggers: list[TriggerRule] = field(default_factory=list)

    def validate(self) -> None:
        if not self.triggers:
            raise ConfigError(f"markets[{self.slug}] has no triggers")
        for t in self.triggers:
            t.validate(self.slug)


@dataclass(frozen=True)
class Config:
    risk: RiskConfig
    strategy: StrategyConfig
    execution: ExecutionConfig
    endpoints: EndpointsConfig
    news: NewsConfig
    markets: list[MarketConfig]
    state_dir: Path
    log_dir: Path

    # Secrets are read from the environment, never from the YAML.
    private_key: Optional[str] = None
    clob_api_key: Optional[str] = None
    clob_api_secret: Optional[str] = None
    clob_api_passphrase: Optional[str] = None
    funder_address: Optional[str] = None
    signature_type: int = 1

    def enabled_markets(self) -> list[MarketConfig]:
        return [m for m in self.markets if m.enabled]

    def require_live_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("POLYMARKET_PRIVATE_KEY", self.private_key),
                ("POLYMARKET_FUNDER_ADDRESS", self.funder_address),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "live mode needs these environment variables: " + ", ".join(missing)
            )


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _build(cls, data: Any, path: str):
    """Construct a dataclass from a dict, rejecting unknown keys."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping, got {type(data).__name__}")
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {sorted(unknown)}")
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if is_dataclass(f.type) and isinstance(value, dict):  # pragma: no cover
            value = _build(f.type, value, f"{path}.{f.name}")
        kwargs[f.name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _normalize_outcome(trigger_raw: Any) -> Any:
    """Rescue `outcome: YES` from YAML 1.1.

    PyYAML resolves unquoted YES/NO/ON/OFF to booleans, so `outcome: YES`
    arrives here as `True`. Rather than demanding quotes and letting a silent
    misparse reach the trading loop, we accept the boolean and convert it.
    """
    if not isinstance(trigger_raw, dict) or "outcome" not in trigger_raw:
        return trigger_raw
    value = trigger_raw["outcome"]
    if isinstance(value, bool):
        value = "YES" if value else "NO"
    elif isinstance(value, str):
        value = value.strip().upper()
    return {**trigger_raw, "outcome": value}


def load_config(path: str | Path = "config.yaml") -> Config:
    """Read config.yaml + .env into a validated Config."""
    load_dotenv()
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path.resolve()}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a mapping at the top level")

    allowed_top = {
        "risk", "strategy", "execution", "endpoints", "news", "markets",
        "state_dir", "log_dir",
    }
    unknown = set(raw) - allowed_top
    if unknown:
        raise ConfigError(f"config.yaml: unknown top-level key(s) {sorted(unknown)}")

    risk = _build(RiskConfig, raw.get("risk"), "risk")
    strategy = _build(StrategyConfig, raw.get("strategy"), "strategy")
    execution = _build(ExecutionConfig, raw.get("execution"), "execution")
    endpoints = _build(EndpointsConfig, raw.get("endpoints"), "endpoints")

    news_raw = dict(raw.get("news") or {})
    feeds = [
        _build(FeedConfig, f, f"news.feeds[{i}]")
        for i, f in enumerate(news_raw.pop("feeds", []) or [])
    ]
    news = _build(NewsConfig, news_raw, "news")
    news = NewsConfig(
        feeds=feeds,
        max_headlines_per_poll=news.max_headlines_per_poll,
        dedupe_cache_size=news.dedupe_cache_size,
        ignore_older_than_sec=news.ignore_older_than_sec,
    )

    markets: list[MarketConfig] = []
    for i, m in enumerate(raw.get("markets") or []):
        m = dict(m)
        triggers = [
            _build(TriggerRule, _normalize_outcome(t), f"markets[{i}].triggers[{j}]")
            for j, t in enumerate(m.pop("triggers", []) or [])
        ]
        base = _build(MarketConfig, m, f"markets[{i}]")
        markets.append(
            MarketConfig(
                slug=base.slug, enabled=base.enabled, note=base.note, triggers=triggers
            )
        )

    risk.validate()
    strategy.validate()
    execution.validate()
    for m in markets:
        if m.enabled:
            m.validate()
    if not markets:
        raise ConfigError("config.yaml defines no markets")
    if not [f for f in feeds if f.enabled]:
        raise ConfigError("config.yaml enables no news feeds")

    root = path.resolve().parent
    state_dir = Path(raw.get("state_dir") or root / "state")
    log_dir = Path(raw.get("log_dir") or root / "logs")
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        risk=risk,
        strategy=strategy,
        execution=execution,
        endpoints=endpoints,
        news=news,
        markets=markets,
        state_dir=state_dir,
        log_dir=log_dir,
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        clob_api_key=os.getenv("POLYMARKET_API_KEY"),
        clob_api_secret=os.getenv("POLYMARKET_API_SECRET"),
        clob_api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
        funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS"),
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1")),
    )

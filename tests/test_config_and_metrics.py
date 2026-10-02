"""Config validation must fail loudly, and metrics must not flatter us."""

from __future__ import annotations

import textwrap

import pytest

from polylag.config import ConfigError, RiskConfig, StrategyConfig, load_config
from polylag.metrics import TradeRow, analyse, max_drawdown


def write_config(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


BASE = """
    risk:
      starting_bankroll_usdc: 600.0
    strategy: {}
    execution: {}
    news:
      feeds:
        - name: t
          url: https://example.test/rss
    markets:
      - slug: some-market
        triggers:
          - name: t1
            outcome: YES
            target_price: 0.9
            confidence: 0.5
            any_of: ["fed cuts"]
"""


def test_valid_config_loads(tmp_path):
    cfg = load_config(write_config(tmp_path, BASE))
    assert cfg.markets[0].triggers[0].name == "t1"
    assert cfg.risk.max_open_positions == 3


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_config(tmp_path, BASE + "    typo_section: 1\n"))


def test_drawdown_must_exceed_daily_limit():
    with pytest.raises(ConfigError, match="max_drawdown_pct"):
        RiskConfig(daily_loss_limit_pct=5.0, max_drawdown_pct=4.0).validate()


def test_equity_floor_must_sit_below_the_bankroll():
    with pytest.raises(ConfigError, match="min_equity_floor_usdc"):
        RiskConfig(starting_bankroll_usdc=300.0).validate()  # floor defaults to 400


def test_absurd_position_size_is_rejected():
    with pytest.raises(ConfigError):
        RiskConfig(max_trade_pct_of_equity=50.0).validate()


def test_min_edge_must_exceed_cost_buffer():
    with pytest.raises(ConfigError, match="min_edge"):
        StrategyConfig(min_edge=0.01, cost_buffer=0.02).validate()


def test_missing_trigger_is_rejected(tmp_path):
    body = BASE.replace(
        """          - name: t1
            outcome: YES
            target_price: 0.9
            confidence: 0.5
            any_of: ["fed cuts"]
""",
        "",
    )
    with pytest.raises(ConfigError, match="no triggers"):
        load_config(write_config(tmp_path, body))


# -- metrics ----------------------------------------------------------------- #


def row(net: float, gross: float | None = None, fees: float = 0.05) -> TradeRow:
    return TradeRow(
        market_slug="m", outcome="YES", shares=100, entry_price=0.6,
        exit_price=0.6 + net / 100, fees_usdc=fees,
        gross_pnl=gross if gross is not None else net + fees,
        net_pnl=net, hold_sec=120, exit_reason="take_profit", trigger="t",
        mode="paper",
    )


def test_max_drawdown_measures_peak_to_trough():
    assert max_drawdown([5, -3, -4, 10]) == -7


def test_expectancy_is_negative_when_fees_eat_the_edge():
    # 7 small wins, 3 losses that are each bigger than a win.
    rows = [row(0.10) for _ in range(7)] + [row(-0.40) for _ in range(3)]
    report = analyse(rows)
    assert report.win_rate == 0.7            # looks great
    assert report.expectancy_usdc < 0        # is not great
    assert "not a sample" in report.verdict  # and is not conclusive either


def test_small_samples_never_get_a_positive_verdict():
    report = analyse([row(1.0) for _ in range(10)])
    assert report.expectancy_usdc > 0
    assert "not a sample" in report.verdict


def test_deteriorating_edge_is_flagged():
    rows = [row(1.0) for _ in range(60)] + [row(-0.5) for _ in range(25)]
    report = analyse(rows, recent_window=25)
    assert report.edge_health["deteriorating"] is True


def test_empty_history_is_handled():
    assert analyse([]).trades == 0

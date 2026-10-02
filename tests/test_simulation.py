"""End-to-end tests: the real engine, driven by scripted markets.

These are slower than the rest of the suite (they run in real time, because the
staleness gates use the wall clock and faking that would stop testing the thing
we care about). They are worth it: everything else tests a component, this tests
that the components are wired together correctly.

`python run.py simulate` runs the full scenario set with commentary; these two
are the fast subset that belongs in CI.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from polylag.simulate import Scenario, run_scenario, scenario_by_name


@pytest.fixture
def sim_dir(tmp_path):
    return tmp_path / "sims"


def test_full_lifecycle_entry_to_take_profit(cfg, sim_dir):
    """News -> match -> gates -> risk -> fill -> exit -> journal, for real."""
    scenario = replace(
        scenario_by_name("clean-lag"),
        warmup_sec=3.0,      # must exceed the 2s pre-news anchor lookback
        runtime_sec=7.0,
        price_path=[(1.0, 0.75), (2.0, 0.86)],
    )
    result = asyncio.run(run_scenario(cfg, scenario, sim_dir))

    assert result.passed, result.detail
    assert result.entered
    assert "take profit" in result.exit_tag
    assert result.net_pnl > 0
    # Equity must have actually moved -- proves the portfolio was updated from a
    # confirmed fill rather than the trade being recorded in name only.
    assert result.equity > cfg.risk.starting_bankroll_usdc


def test_engine_declines_a_market_that_already_moved(cfg, sim_dir):
    """The core discipline: if someone was faster, do not chase them."""
    scenario = replace(
        scenario_by_name("already-repriced"),
        warmup_sec=3.5,
        runtime_sec=4.0,
    )
    result = asyncio.run(run_scenario(cfg, scenario, sim_dir))

    assert result.passed, result.detail
    assert not result.entered
    assert "already_repriced" in result.rejections
    assert result.equity == pytest.approx(cfg.risk.starting_bankroll_usdc)


def test_scenarios_are_all_well_formed():
    """Every shipped scenario must assert something, or it proves nothing."""
    from polylag.simulate import SCENARIOS

    assert SCENARIOS
    for scenario in SCENARIOS:
        assert scenario.expectation, f"{scenario.name} has no stated expectation"
        asserts_something = (
            scenario.expect_exit_tag
            or scenario.expect_reject_gate
            or not scenario.expect_position
        )
        assert asserts_something, f"{scenario.name} asserts nothing"

#!/usr/bin/env python
"""polylag CLI.

    python run.py selftest          unit tests + full offline simulation
    python run.py simulate          drive the engine with scripted markets
    python run.py doctor            connectivity, credentials, balances
    python run.py scan "fed"        find market slugs to put in config.yaml
    python run.py watch             read-only: log signals, place nothing
    python run.py paper             simulated fills against the live book
    python run.py live              REAL MONEY (requires explicit confirmation)
    python run.py status            current risk state
    python run.py report            performance from logs/trades.csv
    python run.py kill "reason"     latch the kill switch right now
    python run.py resume            clear the latch after you have reviewed

Progression is meant to be walked in order: doctor -> scan -> watch -> paper ->
live. Skipping steps is how you find out about a bug with real capital.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from polylag.clients.clob import ClobReadClient
from polylag.clients.gamma import GammaClient
from polylag.config import Config, ConfigError, load_config
from polylag.engine import Engine
from polylag.journal import StateStore
from polylag.logging_setup import setup_logging
from polylag.metrics import analyse, format_report, load_trades
from polylag.portfolio import Portfolio
from polylag.risk.manager import RiskManager, sizing_feasible

log = logging.getLogger("cli")

LIVE_CONFIRMATION = "TRADE LIVE"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


async def cmd_doctor(cfg: Config) -> int:
    print("\n--- polylag doctor ---\n")
    ok = True

    clob = ClobReadClient(cfg.endpoints.clob, cfg.endpoints.http_timeout_sec)
    gamma = GammaClient(cfg.endpoints.gamma, cfg.endpoints.http_timeout_sec)
    try:
        healthy = await clob.health()
        print(f"[{'OK ' if healthy else 'FAIL'}] CLOB reachable at {cfg.endpoints.clob}")
        ok &= healthy

        for market_cfg in cfg.enabled_markets():
            ref = await gamma.resolve_slug(market_cfg.slug)
            if ref is None:
                print(f"[FAIL] market '{market_cfg.slug}' did not resolve")
                ok = False
                continue
            book = await clob.get_book(ref.yes_token_id)
            mid = book.mid if book else None
            ttr = ref.seconds_to_resolution()
            print(
                f"[OK ] {market_cfg.slug}\n"
                f"       {ref.question[:70]}\n"
                f"       YES mid {mid if mid is None else f'{mid:.3f}'} | "
                f"spread {book.spread if book and book.spread else 'n/a'} | "
                f"resolves in {'unknown' if ttr is None else f'{ttr / 3600:.1f}h'} | "
                f"tick {ref.min_tick} | min size ${ref.min_order_size}"
            )
            print(f"       {len(market_cfg.triggers)} trigger(s) configured")

            feasible, note = sizing_feasible(
                cfg.risk, cfg.risk.starting_bankroll_usdc, ref.min_order_size
            )
            print(f"       [{'OK ' if feasible else 'FAIL'}] sizing: {note}")
            ok &= feasible
    finally:
        await clob.close()
        await gamma.close()

    feeds = [f for f in cfg.news.feeds if f.enabled]
    print(f"\n[{'OK ' if feeds else 'FAIL'}] {len(feeds)} news feed(s) enabled")
    for feed in feeds:
        print(f"       {feed.name:<14} every {feed.poll_sec:.0f}s  {feed.url}")

    state = StateStore(cfg.state_dir)
    if state.is_killed():
        print(f"\n[STOP] kill switch is LATCHED: {state.kill_reason()}")
        print(f"       delete {state.kill_path} to resume")
        ok = False
    else:
        print("\n[OK ] kill switch clear")

    print(f"\n[--] execution mode in config: {cfg.execution.mode}")
    print(f"[--] fee assumption: {cfg.execution.fee_bps} bps "
          "(verify against the venue's current schedule)")

    if cfg.private_key and cfg.funder_address:
        print("[OK ] live credentials present in .env")
        try:
            from polylag.execution.live import LiveBroker

            broker = LiveBroker(cfg)
            await broker.connect()
            good, message = await broker.preflight()
            print(f"[{'OK ' if good else 'FAIL'}] {message}")
            ok &= good
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] live broker: {exc}")
            ok = False
    else:
        print("[--] no live credentials in .env (paper/watch only)")

    print(f"\n{'ALL CHECKS PASSED' if ok else 'PROBLEMS FOUND -- fix before trading'}\n")
    return 0 if ok else 1


async def cmd_scan(cfg: Config, query: str, limit: int) -> int:
    gamma = GammaClient(cfg.endpoints.gamma, cfg.endpoints.http_timeout_sec)
    clob = ClobReadClient(cfg.endpoints.clob, cfg.endpoints.http_timeout_sec)
    try:
        results = await gamma.search(query)
        if not results:
            print(f"no open markets matched {query!r}")
            return 1
        print(f"\n{len(results)} match(es) for {query!r}:\n")
        for ref in results[:limit]:
            book = await clob.get_book(ref.yes_token_id)
            mid = f"{book.mid:.3f}" if book and book.mid else "n/a"
            spread = f"{book.spread:.3f}" if book and book.spread else "n/a"
            ttr = ref.seconds_to_resolution()
            print(f"  slug:     {ref.slug}")
            print(f"  question: {ref.question[:78]}")
            print(f"  YES mid:  {mid}   spread: {spread}   "
                  f"resolves in: {'?' if ttr is None else f'{ttr / 3600:.1f}h'}")
            print()
        print("Copy a slug into config.yaml under `markets:` and write its triggers.\n")
    finally:
        await gamma.close()
        await clob.close()
    return 0


async def cmd_run(cfg: Config, mode: str) -> int:
    engine = Engine(cfg, mode)
    try:
        await engine.run()
    except KeyboardInterrupt:
        await engine.shutdown("keyboard interrupt")
        await engine._teardown()  # noqa: SLF001 - deliberate, teardown is idempotent
    except SystemExit as exc:
        print(f"\n{exc}\n")
        return 1
    return 0


async def cmd_simulate(cfg: Config, only: str | None) -> int:
    from polylag.simulate import format_results, run_all

    sim_dir = Path(cfg.state_dir).parent / "simulations"
    print("\nRunning the full engine against scripted markets.")
    print("Nothing touches the network, your state directory, or any real money.\n")
    results = await run_all(cfg, sim_dir, only)
    print(format_results(results))
    return 0 if all(r.passed for r in results) else 1


async def cmd_selftest(cfg: Config) -> int:
    """Everything that can be checked without the venue: tests + simulation."""
    import subprocess

    print("\n[1/2] unit tests\n")
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"],
        cwd=Path(__file__).parent,
    )
    if tests.returncode != 0:
        print("\nunit tests FAILED -- stop here, do not trade\n")
        return 1

    print("\n[2/2] end-to-end simulation\n")
    return await cmd_simulate(cfg, None)


def cmd_status(cfg: Config) -> int:
    state = StateStore(cfg.state_dir)
    portfolio = Portfolio(cfg.risk.starting_bankroll_usdc)
    risk = RiskManager(cfg, state, portfolio)
    status = risk.status(portfolio.equity({}))
    print("\n--- risk state (from disk) ---\n")
    for key, value in status.items():
        print(f"  {key:<22} {value}")
    print()
    if status["killed"]:
        print(f"KILL SWITCH LATCHED. Delete {state.kill_path} to resume.\n")
    return 0


def cmd_report(cfg: Config, mode: str | None) -> int:
    rows = load_trades(Path(cfg.log_dir) / "trades.csv", mode)
    print(format_report(analyse(rows)))
    if mode:
        print(f"(filtered to mode={mode})")
    return 0


def cmd_kill(cfg: Config, reason: str) -> int:
    StateStore(cfg.state_dir).engage_kill(f"manual: {reason}")
    print("kill switch latched. Running instances will flatten and stop.")
    return 0


def cmd_resume(cfg: Config) -> int:
    state = StateStore(cfg.state_dir)
    if not state.is_killed():
        print("kill switch is already clear.")
        return 0
    print(f"latched reason: {state.kill_reason()}")
    answer = input("Have you reconciled positions on Polymarket? type YES: ").strip()
    if answer != "YES":
        print("aborted; latch left in place.")
        return 1
    state.clear_kill()
    print("kill switch cleared.")
    return 0


def confirm_live(cfg: Config, skip: bool) -> bool:
    print("\n" + "=" * 66)
    print("  LIVE TRADING -- REAL MONEY")
    print("=" * 66)
    print(f"  per-trade cap      ${cfg.risk.max_notional_per_trade_usdc:.2f} "
          f"/ {cfg.risk.max_trade_pct_of_equity:.2f}% of equity")
    print(f"  max open positions {cfg.risk.max_open_positions}")
    print(f"  daily loss halt    {cfg.risk.daily_loss_limit_pct:.2f}%")
    print(f"  drawdown kill      {cfg.risk.max_drawdown_pct:.2f}% from peak")
    print(f"  equity floor kill  ${cfg.risk.min_equity_floor_usdc:.2f}")
    print("=" * 66)
    print("  This strategy can and often does lose money. Edges of this kind")
    print("  decay quickly. Nothing about this software predicts profit.")
    print("=" * 66 + "\n")
    if skip:
        print("(--yes supplied; skipping the typed confirmation)\n")
        return True
    return input(f'Type "{LIVE_CONFIRMATION}" to continue: ').strip() == LIVE_CONFIRMATION


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Polymarket news-lag trading system"
    )
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check connectivity, markets, credentials")

    scan = sub.add_parser("scan", help="search open markets by keyword")
    scan.add_argument("query")
    scan.add_argument("--limit", type=int, default=15)

    sim = sub.add_parser("simulate", help="run the full engine on scripted markets (offline)")
    sim.add_argument("--scenario", default=None, help="run just one scenario by name")

    sub.add_parser("selftest", help="unit tests + end-to-end simulation")

    sub.add_parser("watch", help="read-only: evaluate and log, place nothing")
    sub.add_parser("paper", help="simulated fills against the live book")

    live = sub.add_parser("live", help="REAL orders with REAL money")
    live.add_argument("--yes", action="store_true",
                      help="skip the typed confirmation (for headless runs)")

    sub.add_parser("status", help="print persisted risk state")

    report = sub.add_parser("report", help="performance metrics from trades.csv")
    report.add_argument("--mode", choices=["paper", "live"], default=None)

    kill = sub.add_parser("kill", help="latch the kill switch now")
    kill.add_argument("reason", nargs="?", default="manual stop")

    sub.add_parser("resume", help="clear the kill latch after review")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_dir, args.log_level)

    if args.command == "doctor":
        return asyncio.run(cmd_doctor(cfg))
    if args.command == "scan":
        return asyncio.run(cmd_scan(cfg, args.query, args.limit))
    if args.command == "simulate":
        return asyncio.run(cmd_simulate(cfg, args.scenario))
    if args.command == "selftest":
        return asyncio.run(cmd_selftest(cfg))
    if args.command == "watch":
        return asyncio.run(cmd_run(cfg, "watch"))
    if args.command == "paper":
        return asyncio.run(cmd_run(cfg, "paper"))
    if args.command == "live":
        try:
            cfg.require_live_credentials()
        except ConfigError as exc:
            print(f"cannot go live: {exc}", file=sys.stderr)
            return 2
        if not confirm_live(cfg, args.yes):
            print("aborted.")
            return 1
        return asyncio.run(cmd_run(cfg, "live"))
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "report":
        return cmd_report(cfg, args.mode)
    if args.command == "kill":
        return cmd_kill(cfg, args.reason)
    if args.command == "resume":
        return cmd_resume(cfg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

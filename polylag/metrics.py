"""Performance analysis from trades.csv.

Win rate is the least useful number in trading and the one everybody quotes. A
strategy that wins 80% of the time and loses 5x on the other 20% is a slow
bankruptcy. What matters:

  expectancy      average net P&L per trade, in dollars AND as a % of notional.
                  If this is not clearly positive, nothing else is interesting.
  profit factor   gross wins / gross losses. Below 1.0 you are paying to play.
  fee drag        fees as a share of gross P&L. On a 3-5 cent edge, fees and
                  slippage routinely eat all of it.
  max drawdown    peak-to-trough on the realised curve. This, not the average,
                  is what you actually have to live through.
  t-statistic     a crude significance check. Under ~30 trades, assume you have
                  learned nothing. Even at 100, a t-stat under 2 is noise.

The `edge_health` section is the "should I stop" signal: it compares the recent
window against the full history. It cannot tell you the edge is alive -- only
that it has visibly deteriorated.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TradeRow:
    market_slug: str
    outcome: str
    shares: float
    entry_price: float
    exit_price: float
    fees_usdc: float
    gross_pnl: float
    net_pnl: float
    hold_sec: float
    exit_reason: str
    trigger: str
    mode: str

    @property
    def notional(self) -> float:
        return self.shares * self.entry_price


def load_trades(path: Path, mode: Optional[str] = None) -> list[TradeRow]:
    if not Path(path).exists():
        return []
    rows: list[TradeRow] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            try:
                row = TradeRow(
                    market_slug=raw.get("market_slug", ""),
                    outcome=raw.get("outcome", ""),
                    shares=float(raw.get("shares") or 0),
                    entry_price=float(raw.get("entry_price") or 0),
                    exit_price=float(raw.get("exit_price") or 0),
                    fees_usdc=float(raw.get("fees_usdc") or 0),
                    gross_pnl=float(raw.get("gross_pnl") or 0),
                    net_pnl=float(raw.get("net_pnl") or 0),
                    hold_sec=float(raw.get("hold_sec") or 0),
                    exit_reason=raw.get("exit_reason", ""),
                    trigger=raw.get("trigger", ""),
                    mode=raw.get("mode", ""),
                )
            except (TypeError, ValueError):
                continue
            if mode and row.mode != mode:
                continue
            rows.append(row)
    return rows


def max_drawdown(pnls: list[float]) -> float:
    """Peak-to-trough of the cumulative realised curve, in currency."""
    equity, peak, worst = 0.0, 0.0, 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return worst


@dataclass
class Report:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    fees: float = 0.0
    fee_drag_pct: float = 0.0
    expectancy_usdc: float = 0.0
    expectancy_pct_notional: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    median_hold_sec: float = 0.0
    t_stat: float = 0.0
    by_exit_reason: dict[str, dict] = field(default_factory=dict)
    by_trigger: dict[str, dict] = field(default_factory=dict)
    edge_health: dict = field(default_factory=dict)
    verdict: str = ""


def _group(rows: list[TradeRow], key) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        bucket = out.setdefault(key(row), {"n": 0, "net": 0.0, "wins": 0})
        bucket["n"] += 1
        bucket["net"] += row.net_pnl
        bucket["wins"] += 1 if row.net_pnl > 0 else 0
    for bucket in out.values():
        bucket["net"] = round(bucket["net"], 4)
        bucket["avg"] = round(bucket["net"] / bucket["n"], 4)
        bucket["win_rate"] = round(bucket["wins"] / bucket["n"], 3)
    return out


def analyse(rows: list[TradeRow], recent_window: int = 25) -> Report:
    report = Report()
    if not rows:
        report.verdict = "no closed trades yet"
        return report

    nets = [r.net_pnl for r in rows]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]

    report.trades = len(rows)
    report.wins = len(wins)
    report.losses = len(losses)
    report.win_rate = round(len(wins) / len(rows), 4)
    report.gross_pnl = round(sum(r.gross_pnl for r in rows), 4)
    report.net_pnl = round(sum(nets), 4)
    report.fees = round(sum(r.fees_usdc for r in rows), 4)
    report.fee_drag_pct = (
        round(report.fees / abs(report.gross_pnl) * 100, 2) if report.gross_pnl else 0.0
    )
    report.expectancy_usdc = round(report.net_pnl / len(rows), 4)

    total_notional = sum(r.notional for r in rows)
    report.expectancy_pct_notional = (
        round(report.net_pnl / total_notional * 100, 3) if total_notional else 0.0
    )
    report.avg_win = round(sum(wins) / len(wins), 4) if wins else 0.0
    report.avg_loss = round(sum(losses) / len(losses), 4) if losses else 0.0
    report.payoff_ratio = (
        round(abs(report.avg_win / report.avg_loss), 3) if report.avg_loss else 0.0
    )
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    report.profit_factor = round(gross_win / gross_loss, 3) if gross_loss else float("inf")
    report.max_drawdown = round(max_drawdown(nets), 4)

    holds = sorted(r.hold_sec for r in rows)
    report.median_hold_sec = round(holds[len(holds) // 2], 1)

    if len(nets) > 1:
        mean = report.net_pnl / len(nets)
        variance = sum((n - mean) ** 2 for n in nets) / (len(nets) - 1)
        stdev = math.sqrt(variance)
        report.t_stat = round(mean / (stdev / math.sqrt(len(nets))), 3) if stdev else 0.0

    report.by_exit_reason = _group(rows, lambda r: r.exit_reason or "unknown")
    report.by_trigger = _group(rows, lambda r: r.trigger or "unknown")

    recent = rows[-recent_window:]
    recent_exp = sum(r.net_pnl for r in recent) / len(recent)
    report.edge_health = {
        "window": len(recent),
        "recent_expectancy": round(recent_exp, 4),
        "all_time_expectancy": report.expectancy_usdc,
        "deteriorating": bool(recent_exp < 0 <= report.expectancy_usdc),
    }
    report.verdict = _verdict(report)
    return report


def _verdict(report: Report) -> str:
    """A blunt, deliberately conservative read of the numbers."""
    if report.trades < 30:
        return (
            f"{report.trades} trades is not a sample. Keep paper trading; "
            "no conclusion is available yet."
        )
    if report.expectancy_usdc <= 0:
        return "Negative expectancy. Stop. The rules as configured lose money."
    if report.t_stat < 2.0:
        return (
            f"Positive expectancy but t-stat {report.t_stat} < 2.0 -- statistically "
            "indistinguishable from luck. Do not scale up."
        )
    if report.edge_health.get("deteriorating"):
        return (
            "Historically positive but the recent window is negative. Treat the "
            "edge as decaying: halve size or stop until it recovers."
        )
    if report.profit_factor < 1.2:
        return (
            f"Profit factor {report.profit_factor} is thin. One bad fill regime or "
            "a fee change flips this negative."
        )
    return (
        "Positive on this sample. This is evidence, not proof -- prediction-market "
        "lags close as more people watch them, and past results here carry little "
        "information about next month."
    )


def format_report(report: Report) -> str:
    lines = [
        "=" * 62,
        "PERFORMANCE REPORT",
        "=" * 62,
        f"Trades              {report.trades}",
        f"Win rate            {report.win_rate:.1%}  ({report.wins}W / {report.losses}L)",
        f"Net P&L             {report.net_pnl:+.2f} USDC",
        f"Gross P&L           {report.gross_pnl:+.2f} USDC",
        f"Fees paid           {report.fees:.2f} USDC  ({report.fee_drag_pct:.1f}% of gross)",
        "-" * 62,
        f"Expectancy/trade    {report.expectancy_usdc:+.4f} USDC "
        f"({report.expectancy_pct_notional:+.3f}% of notional)",
        f"Avg win / avg loss  {report.avg_win:+.3f} / {report.avg_loss:+.3f} "
        f"(payoff {report.payoff_ratio})",
        f"Profit factor       {report.profit_factor}",
        f"Max drawdown        {report.max_drawdown:.2f} USDC",
        f"Median hold         {report.median_hold_sec:.0f}s",
        f"t-statistic         {report.t_stat}",
        "-" * 62,
        "By exit reason:",
    ]
    for reason, stats in sorted(report.by_exit_reason.items()):
        lines.append(
            f"  {reason:<18} n={stats['n']:<4} net={stats['net']:+.2f} "
            f"avg={stats['avg']:+.3f} win={stats['win_rate']:.0%}"
        )
    lines.append("By trigger:")
    for trigger, stats in sorted(report.by_trigger.items()):
        lines.append(
            f"  {trigger:<18} n={stats['n']:<4} net={stats['net']:+.2f} "
            f"avg={stats['avg']:+.3f} win={stats['win_rate']:.0%}"
        )
    lines += ["-" * 62, "VERDICT: " + report.verdict, "=" * 62]
    return "\n".join(lines)

"""The decision journal and durable state.

Two responsibilities, both about surviving a restart honestly:

  * `Journal` appends every decision and fill to CSV files you can open in a
    spreadsheet. Rejected signals are recorded with their exact rejection
    reason -- this is the file that later tells you whether the edge is dead.
  * `StateStore` persists peak equity, the day anchor, and the kill latch. If
    the process dies mid-drawdown, restarting must NOT reset your limits.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .models import Fill, Signal

log = logging.getLogger("journal")

DECISION_FIELDS = [
    "ts", "run_id", "stage", "action", "market_slug", "outcome", "token_id",
    "side", "reference_price", "fair_value", "edge", "shares", "limit_price",
    "trigger", "news_title", "news_url", "reason", "mode",
]

FILL_FIELDS = [
    "ts", "run_id", "order_id", "market_slug", "outcome", "token_id", "side",
    "shares", "price", "fee_usdc", "notional", "tag", "mode",
]

TRADE_FIELDS = [
    "opened_ts", "closed_ts", "run_id", "market_slug", "outcome", "shares",
    "entry_price", "exit_price", "fees_usdc", "gross_pnl", "net_pnl",
    "hold_sec", "exit_reason", "trigger", "news_title", "mode",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _CsvAppender:
    def __init__(self, path: Path, fieldnames: list[str]) -> None:
        self.path = path
        self.fieldnames = fieldnames
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=fieldnames).writeheader()

    def append(self, row: dict[str, Any]) -> None:
        clean = {k: row.get(k, "") for k in self.fieldnames}
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=self.fieldnames).writerow(clean)


class Journal:
    """Append-only record of what the system did and why."""

    def __init__(self, log_dir: Path, run_id: str, mode: str) -> None:
        self.run_id = run_id
        self.mode = mode
        self.decisions = _CsvAppender(Path(log_dir) / "decisions.csv", DECISION_FIELDS)
        self.fills = _CsvAppender(Path(log_dir) / "fills.csv", FILL_FIELDS)
        self.trades = _CsvAppender(Path(log_dir) / "trades.csv", TRADE_FIELDS)

    def decision(
        self,
        stage: str,
        action: str,
        reason: str,
        signal: Optional[Signal] = None,
        shares: float | str = "",
        limit_price: float | str = "",
    ) -> None:
        row: dict[str, Any] = {
            "ts": _utc_now(),
            "run_id": self.run_id,
            "stage": stage,
            "action": action,
            "reason": reason,
            "shares": shares,
            "limit_price": limit_price,
            "mode": self.mode,
        }
        if signal is not None:
            row.update(
                market_slug=signal.market.slug,
                outcome=signal.outcome,
                token_id=signal.token_id,
                side=signal.side,
                reference_price=round(signal.reference_price, 4),
                fair_value=round(signal.fair_value, 4),
                edge=round(signal.edge, 4),
                trigger=signal.trigger_name,
                news_title=signal.news_title[:200],
            )
        self.decisions.append(row)

    def fill(self, fill: Fill, market_slug: str, outcome: str, tag: str) -> None:
        self.fills.append(
            {
                "ts": _utc_now(),
                "run_id": self.run_id,
                "order_id": fill.order_id,
                "market_slug": market_slug,
                "outcome": outcome,
                "token_id": fill.token_id,
                "side": fill.side,
                "shares": round(fill.shares, 4),
                "price": round(fill.price, 4),
                "fee_usdc": round(fill.fee_usdc, 6),
                "notional": round(fill.shares * fill.price, 4),
                "tag": tag,
                "mode": self.mode,
            }
        )

    def closed_trade(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row.setdefault("run_id", self.run_id)
        row.setdefault("mode", self.mode)
        self.trades.append(row)


class StateStore:
    """Durable risk state. Losing this file resets your drawdown limit, so it is
    written atomically and read on every start."""

    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.kill_path = self.dir / "KILL"
        self.data: dict[str, Any] = self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("state file unreadable (%s); refusing to guess", exc)
            raise

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)  # atomic on POSIX and Windows

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    # -- kill latch --------------------------------------------------------- #

    def is_killed(self) -> bool:
        return self.kill_path.exists()

    def kill_reason(self) -> str:
        if not self.kill_path.exists():
            return ""
        try:
            return self.kill_path.read_text(encoding="utf-8").strip()
        except OSError:
            return "unreadable"

    def engage_kill(self, reason: str) -> None:
        """Latch the kill switch on disk. Survives crashes and restarts."""
        payload = f"{_utc_now()} {reason}"
        self.kill_path.write_text(payload, encoding="utf-8")
        log.critical("KILL SWITCH ENGAGED: %s", reason)

    def clear_kill(self) -> None:
        if self.kill_path.exists():
            self.kill_path.unlink()

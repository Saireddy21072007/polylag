"""Logging: human-readable console + machine-readable JSONL.

Two sinks on purpose.
  - console: what you watch while it runs.
  - logs/events-YYYYMMDD.jsonl: what you analyse afterwards. Every decision,
    including every REJECTED one, lands here. Rejections are the dataset that
    tells you whether the edge ever existed.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CONSOLE_FMT = "%(asctime)s %(levelname)-7s %(name)-18s %(message)s"


class _JsonlHandler(logging.Handler):
    """Writes one JSON object per line, flushing immediately.

    Immediate flush costs a little throughput and buys you a complete log if the
    process is killed mid-trade, which is exactly when you need it.
    """

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }
            extra = getattr(record, "data", None)
            if isinstance(extra, dict):
                payload["data"] = extra
            if record.exc_info:
                payload["exc"] = self.format(record)
            self._fh.write(json.dumps(payload, default=str) + "\n")
            self._fh.flush()
        except Exception:  # never let logging kill the trading loop
            self.handleError(record)

    def close(self) -> None:
        try:
            self._fh.close()
        finally:
            super().close()


def setup_logging(log_dir: Path, level: str = "INFO", quiet_console: bool = False) -> Path:
    """Configure root logging. Returns the JSONL path in use."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    jsonl_path = Path(log_dir) / f"events-{day}.jsonl"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(_CONSOLE_FMT, datefmt="%H:%M:%S"))
    if not quiet_console:
        root.addHandler(console)

    root.addHandler(_JsonlHandler(jsonl_path))

    # Third-party libraries are chatty at DEBUG and hide our own messages.
    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return jsonl_path


def log_event(logger: logging.Logger, level: int, msg: str, **data: Any) -> None:
    """Log a line with structured fields attached for the JSONL sink."""
    logger.log(level, msg, extra={"data": data})

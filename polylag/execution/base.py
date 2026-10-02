"""The broker interface and the fee model.

Paper and live implement the exact same interface so the engine has no idea
which one it is talking to. That is what makes "paper results" meaningful: the
only thing that differs between a paper run and a live run is this one object.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Literal, Optional

from ..models import Fill, OrderIntent

ExecStatus = Literal["filled", "partial", "rejected", "unknown"]


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecStatus
    message: str
    fill: Optional[Fill] = None
    order_id: str = ""

    @property
    def any_fill(self) -> bool:
        return self.fill is not None and self.fill.shares > 1e-9


def taker_fee(shares: float, price: float, fee_bps: float) -> float:
    """Fee in USDC for a taker fill.

    Polymarket's published fee formula is proportional to the *cheaper* side of
    the binary, i.e. `rate * min(p, 1-p) * shares`, so a fill at 0.97 is charged
    on 0.03. Set `execution.fee_bps` from the venue's current published schedule
    and re-check it periodically -- a fee change turns a marginal edge negative
    without any warning, and defaulting this to zero is exactly the kind of
    optimism that loses money.
    """
    if fee_bps <= 0:
        return 0.0
    return (fee_bps / 10_000.0) * min(price, 1.0 - price) * shares


class Broker(abc.ABC):
    """Everything the engine is allowed to ask an execution venue to do."""

    name: str = "broker"
    is_live: bool = False

    @abc.abstractmethod
    async def buy(self, intent: OrderIntent) -> ExecutionResult:
        ...

    @abc.abstractmethod
    async def sell(self, intent: OrderIntent) -> ExecutionResult:
        ...

    async def cancel_all(self) -> None:
        """Cancel every resting order. No-op for fill-or-kill styles."""
        return None

    async def preflight(self) -> tuple[bool, str]:
        """Checked once at startup: can this broker actually trade?"""
        return True, "ok"

    async def close(self) -> None:
        return None

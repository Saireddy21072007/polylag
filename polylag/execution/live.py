"""Live broker -- real signed orders against the Polymarket CLOB.

This is the only file in the repository that can spend money. It is written to
be paranoid rather than clever.

Rules encoded here:

  * Marketable LIMIT orders only, FAK/FOK by default. We never rest a resting
    order carrying a stale opinion, and we never send an unbounded market order
    into a thin book.
  * Every fill is RECONCILED against the venue's own trade records before the
    portfolio is updated. If the response is ambiguous we return `unknown`, and
    the engine treats unknown as a hard stop -- it will not guess a position.
  * Nothing here reads config for *whether* to trade. The engine and the risk
    manager decide; this class only executes what it is handed.

Setup (one time, done by you, not by this program):
  1. Fund your Polymarket account with USDC on Polygon.
  2. Export the private key of the wallet that controls it into .env as
     POLYMARKET_PRIVATE_KEY, and the funder/proxy address as
     POLYMARKET_FUNDER_ADDRESS. Treat that key like cash -- anyone with the
     file can drain the account.
  3. Run `python run.py doctor` and confirm balances and allowances.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..config import Config
from ..models import Fill, OrderIntent, now_ms
from .base import Broker, ExecutionResult, taker_fee

log = logging.getLogger("live")


class LiveBrokerUnavailable(RuntimeError):
    """py-clob-client missing or credentials rejected."""


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class LiveBroker(Broker):
    name = "live"
    is_live = True

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client: Any = None
        self._order_type: Any = None
        self._order_args_cls: Any = None
        self._buy_const: Any = None
        self._sell_const: Any = None
        self._address: str = ""

    # -- lifecycle ---------------------------------------------------------- #

    async def connect(self) -> None:
        """Import the SDK, sign in, and derive API credentials."""
        self.cfg.require_live_credentials()
        await asyncio.to_thread(self._connect_blocking)
        log.info("live broker ready for address %s", self._address)

    def _connect_blocking(self) -> None:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL
        except ImportError as exc:  # pragma: no cover - depends on install
            raise LiveBrokerUnavailable(
                "py-clob-client is not installed. `pip install py-clob-client` "
                "or stay in paper mode."
            ) from exc

        self._order_args_cls = OrderArgs
        self._order_type = OrderType
        self._buy_const, self._sell_const = BUY, SELL

        client = ClobClient(
            self.cfg.endpoints.clob,
            key=self.cfg.private_key,
            chain_id=self.cfg.endpoints.chain_id,
            signature_type=self.cfg.signature_type,
            funder=self.cfg.funder_address,
        )

        # Reuse API creds from .env when present; otherwise derive them. Derived
        # creds are deterministic for a given key, so this is safe to repeat.
        if self.cfg.clob_api_key and self.cfg.clob_api_secret and self.cfg.clob_api_passphrase:
            creds = ApiCreds(
                api_key=self.cfg.clob_api_key,
                api_secret=self.cfg.clob_api_secret,
                api_passphrase=self.cfg.clob_api_passphrase,
            )
        else:
            creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        self._client = client
        self._address = getattr(client, "get_address", lambda: "")() or (
            self.cfg.funder_address or ""
        )

    async def preflight(self) -> tuple[bool, str]:
        """Refuse to start live if we cannot see collateral."""
        if self._client is None:
            return False, "live broker not connected"
        try:
            balance = await self.collateral_balance()
        except Exception as exc:  # noqa: BLE001
            return False, f"could not read balance/allowance: {exc}"
        if balance is None:
            return False, "balance/allowance unreadable -- fix before trading live"
        if balance <= 0:
            return False, f"USDC balance is {balance:.2f} -- nothing to trade with"
        return True, f"live broker OK, collateral {balance:.2f} USDC"

    async def collateral_balance(self) -> Optional[float]:
        def _read() -> Optional[float]:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            resp = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            if not isinstance(resp, dict):
                return None
            raw = resp.get("balance")
            if raw is None:
                return None
            # USDC has 6 decimals and the API returns base units.
            return _f(raw) / 1_000_000.0

        return await asyncio.to_thread(_read)

    async def close(self) -> None:
        self._client = None

    # -- orders ------------------------------------------------------------- #

    async def buy(self, intent: OrderIntent) -> ExecutionResult:
        return await self._send(intent, "BUY")

    async def sell(self, intent: OrderIntent) -> ExecutionResult:
        return await self._send(intent, "SELL")

    async def _send(self, intent: OrderIntent, side: str) -> ExecutionResult:
        if self._client is None:
            return ExecutionResult("rejected", "live broker not connected")

        price = round(intent.limit_price, 4)
        size = round(intent.shares, 2)
        if size <= 0:
            return ExecutionResult("rejected", "non-positive size")

        log.info(
            "SENDING LIVE %s %.2f sh of %s @ %.4f (%s)",
            side, size, intent.token_id[:12], price, intent.reason,
        )
        try:
            resp = await asyncio.to_thread(self._post_blocking, intent, side, price, size)
        except Exception as exc:  # noqa: BLE001
            # A transport failure AFTER signing may still have reached the venue.
            # Never assume "no fill" -- force reconciliation.
            log.exception("order submission raised")
            reconciled = await self._reconcile(intent.token_id, side, since_ms=now_ms() - 60_000)
            if reconciled is not None:
                return ExecutionResult(
                    "partial", f"submission raised ({exc}) but a fill was found",
                    fill=reconciled, order_id=reconciled.order_id,
                )
            return ExecutionResult("unknown", f"submission failed ambiguously: {exc}")

        return await self._interpret(resp, intent, side, price)

    def _post_blocking(self, intent: OrderIntent, side: str, price: float, size: float) -> dict:
        args = self._order_args_cls(
            price=price,
            size=size,
            side=self._buy_const if side == "BUY" else self._sell_const,
            token_id=intent.token_id,
        )
        signed = self._client.create_order(args)
        order_type = getattr(self._order_type, self.cfg.execution.order_type)
        return self._client.post_order(signed, order_type)

    async def _interpret(
        self, resp: Any, intent: OrderIntent, side: str, price: float
    ) -> ExecutionResult:
        if not isinstance(resp, dict):
            return ExecutionResult("unknown", f"unparseable response: {resp!r}")

        order_id = str(resp.get("orderID") or resp.get("orderId") or "")
        if not resp.get("success", False):
            msg = str(resp.get("errorMsg") or "rejected by venue")
            return ExecutionResult("rejected", msg, order_id=order_id)

        making = _f(resp.get("makingAmount"))
        taking = _f(resp.get("takingAmount"))
        status = str(resp.get("status") or "").lower()

        # A BUY makes USDC and takes shares; a SELL is the reverse.
        if making > 0 and taking > 0:
            shares = taking if side == "BUY" else making
            notional = making if side == "BUY" else taking
            avg_price = notional / shares if shares > 0 else price
            fill = Fill(
                order_id=order_id or "live",
                token_id=intent.token_id,
                side=side,  # type: ignore[arg-type]
                shares=shares,
                price=avg_price,
                fee_usdc=taker_fee(shares, avg_price, self.cfg.execution.fee_bps),
            )
            filled_all = shares >= intent.shares - 0.01
            return ExecutionResult(
                "filled" if filled_all else "partial",
                f"{side} {shares:.2f} sh @ {avg_price:.4f} (status {status})",
                fill=fill,
                order_id=order_id,
            )

        if status in ("live", "delayed", "unmatched"):
            # FAK/FOK should not leave anything resting; if it did, kill it.
            await self.cancel_all()
            return ExecutionResult(
                "rejected", f"no immediate match (status {status}); cancelled",
                order_id=order_id,
            )

        if status == "matched":
            # Matched but the response gave us no amounts -- go find the truth.
            fill = await self._reconcile(intent.token_id, side, since_ms=now_ms() - 60_000)
            if fill is not None:
                return ExecutionResult("filled", "reconciled from trade history",
                                       fill=fill, order_id=order_id)
            return ExecutionResult(
                "unknown", "venue reported matched but no amounts and no trade found",
                order_id=order_id,
            )

        return ExecutionResult("unknown", f"unrecognised response: {resp}", order_id=order_id)

    async def _reconcile(
        self, token_id: str, side: str, since_ms: int
    ) -> Optional[Fill]:
        """Best-effort: find our recent fills for this token in trade history."""
        def _read() -> Optional[Fill]:
            from py_clob_client.clob_types import TradeParams

            trades = self._client.get_trades(TradeParams(maker_address=self._address))
            if not isinstance(trades, list):
                return None
            total_shares = 0.0
            notional = 0.0
            order_id = ""
            for trade in trades:
                if not isinstance(trade, dict):
                    continue
                if str(trade.get("asset_id") or trade.get("token_id")) != token_id:
                    continue
                if str(trade.get("side", "")).upper() != side:
                    continue
                ts = _f(trade.get("match_time") or trade.get("timestamp")) * 1000
                if ts and ts < since_ms:
                    continue
                shares = _f(trade.get("size"))
                price = _f(trade.get("price"))
                if shares <= 0 or price <= 0:
                    continue
                total_shares += shares
                notional += shares * price
                order_id = str(trade.get("id") or order_id)
            if total_shares <= 0:
                return None
            avg = notional / total_shares
            return Fill(
                order_id=order_id or "reconciled",
                token_id=token_id,
                side=side,  # type: ignore[arg-type]
                shares=total_shares,
                price=avg,
                fee_usdc=taker_fee(total_shares, avg, self.cfg.execution.fee_bps),
            )

        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:  # noqa: BLE001
            log.error("reconciliation failed: %s", exc)
            return None

    async def cancel_all(self) -> None:
        if self._client is None:
            return
        try:
            await asyncio.to_thread(self._client.cancel_all)
            log.info("cancelled all resting orders")
        except Exception as exc:  # noqa: BLE001
            log.error("cancel_all failed: %s", exc)

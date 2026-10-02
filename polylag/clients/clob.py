"""CLOB REST client -- order books and prices (read-only, no auth needed).

This is the price source of record. The websocket in ws.py keeps books fresh in
between; this client provides the initial snapshot and the resync path when the
socket drops.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..models import Level, OrderBook, now_ms
from .http import HttpClient

log = logging.getLogger("clob")


def _levels(raw: Any, reverse: bool) -> list[Level]:
    """Normalise a raw book side into sorted Levels.

    The API returns strings; anything unparseable is dropped rather than
    silently treated as zero.
    """
    out: list[Level] = []
    for item in raw or []:
        try:
            price = float(item["price"])
            size = float(item["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if size > 0 and 0.0 < price < 1.0:
            out.append(Level(price=price, size=size))
    out.sort(key=lambda lv: lv.price, reverse=reverse)
    return out


def book_from_payload(token_id: str, payload: dict) -> OrderBook:
    """Build an OrderBook from either a REST /book response or a WS `book` event."""
    return OrderBook(
        token_id=token_id,
        bids=_levels(payload.get("bids"), reverse=True),   # highest bid first
        asks=_levels(payload.get("asks"), reverse=False),  # lowest ask first
        ts_ms=now_ms(),
    )


class ClobReadClient:
    def __init__(self, base_url: str, timeout: float = 10.0, rate: float = 5.0,
                 burst: int = 10, proxy: Optional[str] = None) -> None:
        self._http = HttpClient(
            base_url, timeout=timeout, rate_per_sec=rate, burst=burst, proxy=proxy
        )

    async def close(self) -> None:
        await self._http.close()

    async def health(self) -> bool:
        try:
            await self._http.get("/ok")
            return True
        except Exception as exc:  # noqa: BLE001 - doctor command reports anything
            log.error("CLOB health check failed: %s", exc)
            return False

    async def get_book(self, token_id: str) -> Optional[OrderBook]:
        payload = await self._http.get("/book", params={"token_id": token_id})
        if not isinstance(payload, dict):
            return None
        return book_from_payload(token_id, payload)

    async def get_books(self, token_ids: list[str]) -> dict[str, OrderBook]:
        """Batch snapshot. One request instead of N keeps us inside rate limits."""
        if not token_ids:
            return {}
        payload = await self._http.post(
            "/books", json_body=[{"token_id": t} for t in token_ids]
        )
        books: dict[str, OrderBook] = {}
        if isinstance(payload, list):
            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                tid = str(entry.get("asset_id") or entry.get("token_id") or "")
                if tid:
                    books[tid] = book_from_payload(tid, entry)
        missing = [t for t in token_ids if t not in books]
        if missing:
            log.warning("batch book fetch missing %d token(s); filling singly",
                        len(missing))
            for tid in missing:
                book = await self.get_book(tid)
                if book:
                    books[tid] = book
        return books

    async def get_midpoint(self, token_id: str) -> Optional[float]:
        payload = await self._http.get("/midpoint", params={"token_id": token_id})
        if isinstance(payload, dict) and "mid" in payload:
            try:
                return float(payload["mid"])
            except (TypeError, ValueError):
                return None
        return None

    async def get_price(self, token_id: str, side: str) -> Optional[float]:
        """Best executable price for `side` ("buy" or "sell")."""
        payload = await self._http.get(
            "/price", params={"token_id": token_id, "side": side.lower()}
        )
        if isinstance(payload, dict) and "price" in payload:
            try:
                return float(payload["price"])
            except (TypeError, ValueError):
                return None
        return None

    async def get_tick_size(self, token_id: str) -> Optional[float]:
        payload = await self._http.get("/tick-size", params={"token_id": token_id})
        if isinstance(payload, dict):
            try:
                return float(payload.get("minimum_tick_size"))
            except (TypeError, ValueError):
                return None
        return None

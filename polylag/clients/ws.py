"""Live market data over the public CLOB websocket.

Design notes worth understanding before you touch this file:

  * The socket is the fast path; REST is the truth. On connect, on reconnect,
    and any time the book looks impossible (crossed, empty), we resync from
    REST. A lag strategy that trades a corrupted local book is just a slower way
    of giving money away.
  * `BookStore` also keeps a short rolling history of mids. That history is what
    lets us answer the only question that matters: "has this market already
    moved since the headline?"
  * Every book carries a timestamp and the risk manager refuses to trade on a
    stale one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections import deque
from typing import Iterable, Optional

import websockets

from ..models import Level, OrderBook, now_ms
from .clob import ClobReadClient, book_from_payload

log = logging.getLogger("ws")

_HISTORY_SECONDS = 900  # 15 minutes of mids is plenty for a lag window


class BookStore:
    """In-memory books + mid history. Single-threaded, asyncio-owned."""

    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}
        self._history: dict[str, deque[tuple[int, float]]] = {}
        self.last_message_ms: int = 0
        self.connected: bool = False

    def set_book(self, book: OrderBook) -> None:
        self._books[book.token_id] = book
        self._record_mid(book)

    def get(self, token_id: str) -> Optional[OrderBook]:
        return self._books.get(token_id)

    def _record_mid(self, book: OrderBook) -> None:
        mid = book.mid
        if mid is None:
            return
        hist = self._history.setdefault(book.token_id, deque())
        hist.append((book.ts_ms, mid))
        cutoff = book.ts_ms - _HISTORY_SECONDS * 1000
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def mid_at_or_before(self, token_id: str, ts_ms: int) -> Optional[float]:
        """The last mid observed at or before `ts_ms`.

        This is the pre-news reference price. If we have no observation from
        before the headline we return None and the strategy declines to trade --
        without a "before" price there is no measurable lag.
        """
        hist = self._history.get(token_id)
        if not hist:
            return None
        best: Optional[float] = None
        for ts, mid in hist:
            if ts <= ts_ms:
                best = mid
            else:
                break
        return best

    def history_len(self, token_id: str) -> int:
        return len(self._history.get(token_id, ()))

    def apply_price_change(self, token_id: str, changes: list[dict]) -> None:
        """Apply incremental level updates. Size is absolute, 0 removes."""
        book = self._books.get(token_id)
        if book is None:
            return  # no snapshot yet; the resync will bring one
        for change in changes:
            try:
                price = float(change["price"])
                size = float(change["size"])
                side = str(change.get("side", "")).upper()
            except (KeyError, TypeError, ValueError):
                continue
            levels = book.bids if side in ("BUY", "BID") else book.asks
            levels[:] = [lv for lv in levels if abs(lv.price - price) > 1e-9]
            if size > 0:
                levels.append(Level(price=price, size=size))
            levels.sort(key=lambda lv: lv.price, reverse=side in ("BUY", "BID"))
        book.ts_ms = now_ms()
        self._record_mid(book)


class MarketFeed:
    """Maintains a websocket subscription to a set of outcome tokens."""

    def __init__(
        self,
        ws_url: str,
        rest: ClobReadClient,
        store: BookStore,
        resync_interval_sec: float = 60.0,
        proxy: Optional[str] = None,
    ) -> None:
        self.ws_url = ws_url
        self.rest = rest
        self.store = store
        self.resync_interval_sec = resync_interval_sec
        self.proxy = proxy
        self._token_ids: list[str] = []
        self._task: Optional[asyncio.Task] = None
        self._resync_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def start(self, token_ids: Iterable[str]) -> None:
        self._token_ids = sorted(set(token_ids))
        if not self._token_ids:
            raise ValueError("MarketFeed needs at least one token id")
        await self.resync()  # REST snapshot before the socket opens
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="ws-feed")
        self._resync_task = asyncio.create_task(self._resync_loop(), name="ws-resync")

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._task, self._resync_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.store.connected = False

    async def resync(self) -> None:
        """Authoritative REST refresh of every watched book."""
        try:
            books = await self.rest.get_books(self._token_ids)
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            log.error("REST resync failed: %s", exc)
            return
        for book in books.values():
            self.store.set_book(book)
        log.debug("resynced %d books", len(books))

    async def _resync_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.resync_interval_sec)
                return
            except asyncio.TimeoutError:
                await self.resync()

    def _connect(self):
        """Open the socket, using a proxy only if this websockets build supports it.

        Proxy support landed in newer websockets releases. Rather than pinning a
        version we ask for it and fall back cleanly, so a proxy-less install
        still works instead of crashing on an unexpected keyword.
        """
        kwargs = dict(ping_interval=10, ping_timeout=20, close_timeout=5, max_queue=1024)
        if self.proxy:
            try:
                return websockets.connect(self.ws_url, proxy=self.proxy, **kwargs)
            except TypeError:
                log.warning(
                    "this websockets version cannot proxy; connecting directly. "
                    "Market data may fail if your network blocks the venue."
                )
        return websockets.connect(self.ws_url, **kwargs)

    async def _run(self) -> None:
        """Connect, subscribe, and pump messages. Reconnects with backoff."""
        attempt = 0
        while not self._stop.is_set():
            try:
                async with self._connect() as sock:
                    await sock.send(
                        json.dumps({"assets_ids": self._token_ids, "type": "market"})
                    )
                    self.store.connected = True
                    self.store.last_message_ms = now_ms()
                    attempt = 0
                    log.info("websocket connected (%d tokens)", len(self._token_ids))
                    await self.resync()  # snapshot again after (re)subscribe
                    async for raw in sock:
                        self.store.last_message_ms = now_ms()
                        self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.store.connected = False
                attempt += 1
                delay = min(30.0, 0.5 * 2 ** attempt) * random.uniform(0.5, 1.0)
                log.warning("websocket dropped (%s); reconnecting in %.1fs", exc, delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    continue
        self.store.connected = False

    def _handle(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("event_type") or event.get("type")
            token_id = str(event.get("asset_id") or event.get("token_id") or "")
            if not token_id:
                continue
            if kind == "book":
                self.store.set_book(book_from_payload(token_id, event))
            elif kind == "price_change":
                changes = event.get("changes")
                if not isinstance(changes, list):
                    changes = [event]  # some payloads carry a single change inline
                self.store.apply_price_change(token_id, changes)
            # last_trade_price / tick_size_change are informational for us.

            book = self.store.get(token_id)
            if book is not None and book.is_crossed():
                log.warning("crossed book on %s; scheduling resync", token_id[:12])
                asyncio.create_task(self.resync())

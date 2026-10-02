"""Polling RSS/Atom feeds into a stream of de-duplicated NewsEvents.

Read this before you trust it
-----------------------------
Public RSS is typically 30 seconds to several minutes behind the wire, and
publishers cache aggressively. That is *slower* than most traders who are
already watching the same story. Treat RSS as a way to measure whether a lag
window exists at all, not as a source of speed.

If you later want a genuinely fast source, the honest options are paid: a
licensed wire feed, an exchange/agency API with push delivery, or a low-latency
social firehose. Swap `Feed._fetch` and everything downstream keeps working --
the module boundary exists exactly for that.

Implementation details that matter:
  * conditional GET (ETag / If-Modified-Since) so we are polite and cheap
  * jittered intervals so all feeds do not fire on the same tick
  * a bounded dedupe set so a restart does not replay yesterday's news
  * the decision clock starts when WE see a headline, not at its stated
    publication time, which is often rounded or wrong
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import random
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

import feedparser
import httpx

from ..config import FeedConfig, NewsConfig
from ..models import NewsEvent, now_ms

log = logging.getLogger("news")

NewsHandler = Callable[[NewsEvent], Awaitable[None]]


def _event_id(source: str, link: str, title: str) -> str:
    basis = link.strip() or title.strip()
    return hashlib.sha256(f"{source}|{basis}".encode("utf-8")).hexdigest()[:16]


def _published_ms(entry) -> Optional[int]:
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None) or entry.get(key)
        if parsed:
            try:
                return int(calendar.timegm(parsed) * 1000)
            except (TypeError, ValueError):
                continue
    return None


class _Dedupe:
    """Bounded LRU set of event ids."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def is_new(self, key: str) -> bool:
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        while len(self._seen) > self.capacity:
            self._seen.popitem(last=False)
        return True


class Feed:
    """One RSS/Atom source."""

    def __init__(self, cfg: FeedConfig, timeout: float = 10.0,
                 proxy: Optional[str] = None) -> None:
        self.cfg = cfg
        self._etag: Optional[str] = None
        self._modified: Optional[str] = None
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "polylag/1.0 (personal research reader)"},
            follow_redirects=True,
            proxy=proxy,
        )
        self.consecutive_errors = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch(self) -> Optional[bytes]:
        headers = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._modified:
            headers["If-Modified-Since"] = self._modified
        resp = await self._client.get(self.cfg.url, headers=headers)
        if resp.status_code == 304:
            return None  # nothing new, cheapest possible poll
        if resp.status_code == 429:
            self.consecutive_errors += 1
            log.warning("feed %s rate limited", self.cfg.name)
            return None
        resp.raise_for_status()
        self._etag = resp.headers.get("ETag", self._etag)
        self._modified = resp.headers.get("Last-Modified", self._modified)
        return resp.content

    async def poll(self, limit: int) -> list[NewsEvent]:
        """Fetch and parse. Returns newest-first entries (still un-deduped)."""
        try:
            body = await self._fetch()
        except (httpx.HTTPError, httpx.TransportError) as exc:
            self.consecutive_errors += 1
            log.warning("feed %s fetch failed (%s)", self.cfg.name, exc)
            return []
        if body is None:
            self.consecutive_errors = 0
            return []

        # feedparser is CPU-bound and synchronous; keep it off the event loop.
        parsed = await asyncio.to_thread(feedparser.parse, body)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            self.consecutive_errors += 1
            log.warning("feed %s unparseable", self.cfg.name)
            return []

        self.consecutive_errors = 0
        seen_ms = now_ms()
        events: list[NewsEvent] = []
        for entry in parsed.entries[:limit]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            link = (entry.get("link") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            events.append(
                NewsEvent(
                    event_id=_event_id(self.cfg.name, link, title),
                    source=self.cfg.name,
                    title=title,
                    summary=summary[:1000],
                    url=link,
                    published_ms=_published_ms(entry),
                    seen_ms=seen_ms,
                )
            )
        return events


class NewsMonitor:
    """Runs every enabled feed on its own schedule and calls `handler`."""

    def __init__(self, cfg: NewsConfig, timeout: float = 10.0,
                 proxy: Optional[str] = None) -> None:
        self.cfg = cfg
        self._feeds = [Feed(f, timeout, proxy) for f in cfg.feeds if f.enabled]
        self._dedupe = _Dedupe(cfg.dedupe_cache_size)
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.warmed_up = False
        self.events_seen = 0

    async def prime(self) -> None:
        """Read every feed once and swallow the results.

        Without this, the first poll of a 40-item feed looks like 40 breaking
        stories and the system would fire on all of them.
        """
        for feed in self._feeds:
            for event in await feed.poll(self.cfg.max_headlines_per_poll):
                self._dedupe.is_new(event.event_id)
        self.warmed_up = True
        log.info("news primed across %d feed(s)", len(self._feeds))

    async def start(self, handler: NewsHandler) -> None:
        if not self.warmed_up:
            await self.prime()
        self._stop.clear()
        for feed in self._feeds:
            self._tasks.append(
                asyncio.create_task(self._loop(feed, handler), name=f"feed-{feed.cfg.name}")
            )

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        for feed in self._feeds:
            await feed.close()

    async def _loop(self, feed: Feed, handler: NewsHandler) -> None:
        # Stagger startup so N feeds do not all hit the network at t=0.
        await asyncio.sleep(random.uniform(0, min(5.0, feed.cfg.poll_sec)))
        while not self._stop.is_set():
            try:
                events = await feed.poll(self.cfg.max_headlines_per_poll)
                for event in events:
                    if not self._dedupe.is_new(event.event_id):
                        continue
                    if self._too_old(event):
                        continue
                    self.events_seen += 1
                    await handler(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad feed must not stop the rest
                log.exception("feed %s loop error", feed.cfg.name)

            # Back off a failing feed instead of hammering it.
            penalty = min(8, feed.consecutive_errors) * feed.cfg.poll_sec
            wait = feed.cfg.poll_sec * random.uniform(0.85, 1.15) + penalty
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                continue

    def _too_old(self, event: NewsEvent) -> bool:
        if event.published_ms is None:
            return False  # no timestamp: judge it on when we saw it
        age = (now_ms() - event.published_ms) / 1000.0
        if age > self.cfg.ignore_older_than_sec:
            log.debug("ignoring stale headline (%.0fs): %s", age, event.title[:80])
            return True
        return False

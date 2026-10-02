"""Gamma API client -- market discovery and reference data (read-only, no auth).

Gamma is Polymarket's public metadata service. We use it for one job: turn a
human-readable market slug from config.yaml into the two CLOB token ids we
actually trade. Prices come from the CLOB, never from here -- Gamma's
`outcomePrices` are cached and are exactly the kind of stale number that makes a
lag strategy hallucinate an edge.

Endpoint shapes change. If a field disappears, `_to_market_ref` is the one place
to fix.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from ..models import MarketRef
from .http import HttpClient

log = logging.getLogger("gamma")


def _parse_iso(value: Any) -> Optional[int]:
    if not value or not isinstance(value, str):
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _maybe_json_list(value: Any) -> list:
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class GammaClient:
    def __init__(self, base_url: str, timeout: float = 10.0, rate: float = 5.0,
                 burst: int = 10, proxy: Optional[str] = None) -> None:
        self._http = HttpClient(
            base_url, timeout=timeout, rate_per_sec=rate, burst=burst, proxy=proxy
        )

    async def close(self) -> None:
        await self._http.close()

    # -- raw fetches --------------------------------------------------------- #

    async def markets_by_slug(self, slug: str) -> list[dict]:
        data = await self._http.get("/markets", params={"slug": slug})
        return data if isinstance(data, list) else []

    async def list_markets(
        self, limit: int = 100, offset: int = 0, closed: bool = False
    ) -> list[dict]:
        data = await self._http.get(
            "/markets",
            params={
                "limit": limit,
                "offset": offset,
                "closed": str(closed).lower(),
                "order": "volumeNum",
                "ascending": "false",
            },
        )
        return data if isinstance(data, list) else []

    # -- typed helpers ------------------------------------------------------- #

    def _to_market_ref(self, raw: dict) -> Optional[MarketRef]:
        token_ids = _maybe_json_list(raw.get("clobTokenIds"))
        outcomes = [str(o).upper() for o in _maybe_json_list(raw.get("outcomes"))]
        if len(token_ids) != 2:
            log.debug("skipping %s: not a binary market", raw.get("slug"))
            return None

        # Do not assume index 0 is YES -- read the outcomes array and only fall
        # back to positional order when the labels are non-standard.
        if "YES" in outcomes and "NO" in outcomes:
            yes_token = str(token_ids[outcomes.index("YES")])
            no_token = str(token_ids[outcomes.index("NO")])
        else:
            yes_token, no_token = str(token_ids[0]), str(token_ids[1])
            log.warning(
                "market %s has non-standard outcomes %s; assuming positional order",
                raw.get("slug"), outcomes,
            )

        end_iso = raw.get("endDate") or raw.get("end_date_iso")
        return MarketRef(
            condition_id=str(raw.get("conditionId") or raw.get("condition_id") or ""),
            question=str(raw.get("question") or ""),
            slug=str(raw.get("slug") or ""),
            yes_token_id=yes_token,
            no_token_id=no_token,
            end_date_iso=end_iso,
            end_ts_ms=_parse_iso(end_iso),
            min_tick=_to_float(raw.get("orderPriceMinTickSize"), 0.01),
            min_order_size=_to_float(raw.get("orderMinSize"), 5.0),
            accepting_orders=bool(raw.get("acceptingOrders", True)),
            closed=bool(raw.get("closed", False)),
        )

    async def resolve_slug(self, slug: str) -> Optional[MarketRef]:
        """Config gives us a slug; this gives us something tradable."""
        raws = await self.markets_by_slug(slug)
        if not raws:
            log.error("no market found for slug %r", slug)
            return None
        ref = self._to_market_ref(raws[0])
        if ref is None:
            return None
        if ref.closed:
            log.error("market %s is closed", slug)
            return None
        if not ref.accepting_orders:
            log.warning("market %s is not accepting orders right now", slug)
        return ref

    async def search(self, needle: str, pages: int = 3, page_size: int = 100) -> list[MarketRef]:
        """Substring search over the most active open markets.

        Client-side filtering keeps us to documented parameters instead of
        relying on an undocumented search endpoint that may change.
        """
        needle = needle.lower()
        found: list[MarketRef] = []
        for page in range(pages):
            raws = await self.list_markets(limit=page_size, offset=page * page_size)
            if not raws:
                break
            for raw in raws:
                text = f"{raw.get('question', '')} {raw.get('slug', '')}".lower()
                if needle in text:
                    ref = self._to_market_ref(raw)
                    if ref and not ref.closed:
                        found.append(ref)
        return found

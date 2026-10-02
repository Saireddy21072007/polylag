"""Shared async HTTP plumbing: one rate-limited, retrying client per host.

Every outbound request in the system goes through here so that rate limiting,
timeouts and 429 handling exist in exactly one place.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from ..ratelimit import RetryableError, RetryPolicy, TokenBucket, with_retries

log = logging.getLogger("http")

# Status codes worth retrying. 429 = rate limited, 5xx = their side.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        rate_per_sec: float = 5.0,
        burst: int = 10,
        attempts: int = 3,
        user_agent: str = "polylag/1.0 (+personal research bot)",
        proxy: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._bucket = TokenBucket(rate_per_sec, burst)
        self._policy = RetryPolicy(attempts=attempts)
        # trust_env=True (httpx default) already honours HTTPS_PROXY/NO_PROXY;
        # an explicit proxy in config.yaml overrides it.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
            proxy=proxy,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict] = None,
    ) -> Any:
        async def once() -> Any:
            await self._bucket.acquire()
            try:
                resp = await self._client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise RetryableError(f"transport error: {exc}") from exc

            if resp.status_code in _RETRYABLE_STATUS:
                retry_after = resp.headers.get("Retry-After")
                seconds: Optional[float]
                try:
                    seconds = float(retry_after) if retry_after else None
                except ValueError:
                    seconds = None
                if resp.status_code == 429:
                    log.warning("rate limited by %s on %s", self.base_url, path)
                raise RetryableError(f"HTTP {resp.status_code} on {path}", seconds)

            if resp.status_code >= 400:
                # 4xx that is not 429 means we sent something wrong. Surface it.
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} on {path}: {resp.text[:400]}",
                    request=resp.request,
                    response=resp,
                )
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

        return await with_retries(once, self._policy, f"{method} {path}")

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

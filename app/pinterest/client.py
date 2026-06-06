"""Persistent httpx client for talking to Pinterest's internal endpoints.

ANONYMOUS by default: on first use it warms up by hitting the Pinterest homepage
so the server hands us baseline cookies (csrftoken, etc.). If PINTEREST_SESS_COOKIE
is set (Phase 2), the session/CSRF cookies from env are injected as well — but the
app works fully with them unset.

Includes:
  - user-agent rotation (per client build)
  - a configurable minimum delay between requests
  - exponential backoff + retry on 429 / 403 / 5xx

Switching Phase 1 <-> Phase 2 is purely a config change (set/unset env vars);
no code change is needed.
"""
from __future__ import annotations

import asyncio
import random
import time

import httpx

from app import config

PINTEREST_BASE = "https://www.pinterest.com"

# A small pool of realistic desktop user-agents to rotate across.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) "
    "Gecko/20100101 Firefox/133.0",
]

# Retry policy for transient blocks / server errors.
_RETRY_STATUSES = {429, 403, 500, 502, 503, 504}
_MAX_RETRIES = 3


class BlockedError(Exception):
    """Raised when Pinterest persistently blocks/rate-limits us.

    Callers should catch this and degrade gracefully (partial data + a clear
    status message) rather than crash.
    """


class PinterestClient:
    """Thin async wrapper around a persistent httpx.AsyncClient."""

    def __init__(self) -> None:
        self._user_agent = random.choice(_USER_AGENTS)
        self._client: httpx.AsyncClient | None = None
        self._warmed_up = False
        self._last_request_ts = 0.0
        self._lock = asyncio.Lock()

    # --- lifecycle ---

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "User-Agent": self._user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                # Browser-like client hints / fetch metadata. These don't defeat
                # bot-protection on their own but make anonymous requests look
                # less like a bare scraper.
                "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1",
            }
            cookies: dict[str, str] = {}
            # Phase 2 lever: inject a logged-in session if configured.
            if config.PINTEREST_SESS_COOKIE:
                cookies["_pinterest_sess"] = config.PINTEREST_SESS_COOKIE
            if config.PINTEREST_CSRF_TOKEN:
                cookies["csrftoken"] = config.PINTEREST_CSRF_TOKEN

            self._client = httpx.AsyncClient(
                base_url=PINTEREST_BASE,
                headers=headers,
                cookies=cookies,
                timeout=httpx.Timeout(20.0),
                follow_redirects=True,
                http2=True,
            )
        return self._client

    async def warmup(self) -> None:
        """Hit the homepage once so Pinterest issues baseline cookies."""
        if self._warmed_up:
            return
        client = await self._ensure_client()
        try:
            await self._throttle()
            resp = await client.get("/", headers={"Accept": "text/html"})
            # Even a non-200 still usually sets cookies; don't hard-fail here.
            _ = resp.status_code
        except httpx.HTTPError:
            # Warmup is best-effort; the real request may still work.
            pass
        finally:
            self._warmed_up = True

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- request plumbing ---

    async def _throttle(self) -> None:
        """Enforce a minimum delay between outbound requests."""
        delay = config.PINTEREST_REQUEST_DELAY
        if delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request_ts = time.monotonic()

    def _csrf_header(self, client: httpx.AsyncClient) -> dict[str, str]:
        token = client.cookies.get("csrftoken")
        return {"X-CSRFToken": token} if token else {}

    async def get_resource(
        self,
        resource_path: str,
        *,
        source_url: str,
        data: str,
    ) -> httpx.Response:
        """GET a Pinterest `/resource/.../get/` endpoint with retry/backoff.

        `data` is the url-encodable JSON string the resource expects; httpx
        handles encoding via the params dict.
        """
        async with self._lock:
            await self.warmup()
            client = await self._ensure_client()
            headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": PINTEREST_BASE + source_url,
                "Origin": PINTEREST_BASE,
                # Headers a real Pinterest web XHR sends. The API path is the
                # most bot-protected surface; without these it 403s even when
                # authenticated.
                "X-APP-VERSION": "0c2c1f6",
                "X-Pinterest-PWS-Handler": "www/[username]/[slug].js",
                "X-Pinterest-AppState": "active",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                **self._csrf_header(client),
            }
            params = {"source_url": source_url, "data": data}

            last_exc: Exception | None = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    await self._throttle()
                    resp = await client.get(
                        resource_path, params=params, headers=headers
                    )
                except httpx.HTTPError as exc:  # network-level failure
                    last_exc = exc
                else:
                    if resp.status_code not in _RETRY_STATUSES:
                        return resp
                    last_exc = BlockedError(
                        f"Pinterest returned {resp.status_code}"
                    )

                # Backoff before the next attempt (skip after the last one).
                if attempt < _MAX_RETRIES:
                    backoff = (2 ** attempt) + random.uniform(0, 0.5)
                    await asyncio.sleep(backoff)

            raise BlockedError(
                f"Request to {resource_path} failed after "
                f"{_MAX_RETRIES + 1} attempts: {last_exc}"
            )

    async def get_url(self, url: str) -> httpx.Response:
        """Plain GET (used to resolve pin.it short links)."""
        async with self._lock:
            client = await self._ensure_client()
            await self._throttle()
            return await client.get(url)

    async def get_with_retry(
        self, url: str, *, accept: str | None = None
    ) -> httpx.Response:
        """GET an arbitrary URL with the same throttle + backoff as the
        resource API. Used by the fallback fetchers (oEmbed, HTML page).

        Returns the final response (caller inspects status); raises
        BlockedError only on persistent network failure.
        """
        async with self._lock:
            await self.warmup()
            client = await self._ensure_client()
            headers = {"Accept": accept} if accept else {}

            last_exc: Exception | None = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    await self._throttle()
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    last_exc = exc
                else:
                    if resp.status_code not in _RETRY_STATUSES:
                        return resp
                    last_exc = BlockedError(f"HTTP {resp.status_code}")

                if attempt < _MAX_RETRIES:
                    backoff = (2 ** attempt) + random.uniform(0, 0.5)
                    await asyncio.sleep(backoff)

            raise BlockedError(f"GET {url} failed after retries: {last_exc}")


# Single shared instance for the app's lifetime (persistent session).
_shared_client: PinterestClient | None = None


def get_client() -> PinterestClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = PinterestClient()
    return _shared_client


async def close_client() -> None:
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None

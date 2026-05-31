"""Pin-detail fetch + parse.

Step 3 scope: resolve a pin URL to an ID, build the PinResource request, fetch
it anonymously, and return the RAW JSON so we can confirm true field paths
before writing the defensive mapper in Step 4.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.pinterest.client import BlockedError, PinterestClient

PIN_RESOURCE_PATH = "/resource/PinResource/get/"

# Matches /pin/<digits>/ in a full or partial Pinterest URL.
_PIN_ID_RE = re.compile(r"/pin/(\d+)")
_BARE_ID_RE = re.compile(r"^\d+$")


class PinUrlError(ValueError):
    """The provided string is not a recognizable Pinterest pin URL/ID."""


async def resolve_pin_id(raw: str, client: PinterestClient) -> str:
    """Extract a numeric pin id from a URL, short link, or bare id.

    Handles pin.it short links by following the redirect.
    """
    raw = (raw or "").strip()
    if not raw:
        raise PinUrlError("Empty input.")

    if _BARE_ID_RE.match(raw):
        return raw

    m = _PIN_ID_RE.search(raw)
    if m:
        return m.group(1)

    # pin.it short link (or any other) -> resolve to the canonical pin.
    # pin.it serves a JS/app interstitial (HTTP 200, no Location header) rather
    # than a plain 3xx, so we check, in order: the final URL after redirects,
    # then the response body (og:url / canonical / any /pin/<id> reference).
    if "pin.it" in raw or raw.startswith("http"):
        try:
            resp = await client.get_url(raw)
        except Exception as exc:  # noqa: BLE001 - surface as a clean error
            raise PinUrlError(f"Could not resolve link: {exc}") from exc

        # 1. canonical URL after any HTTP redirects
        m = _PIN_ID_RE.search(str(resp.url))
        if m:
            return m.group(1)

        # 2. dig the pin id out of the interstitial HTML body
        body = resp.text or ""
        for pat in (
            r'og:url["\'][^>]*content=["\'][^"\']*?/pin/(\d+)',
            r'rel=["\']canonical["\'][^>]*href=["\'][^"\']*?/pin/(\d+)',
            _PIN_ID_RE.pattern,
        ):
            bm = re.search(pat, body)
            if bm:
                return bm.group(1)

    raise PinUrlError("Could not find a pin id in that input.")


def build_pin_data_param(pin_id: str) -> str:
    """Build the `data` query param for the PinResource request."""
    payload = {
        "options": {"id": pin_id, "field_set_key": "detailed"},
        "context": {},
    }
    return json.dumps(payload, separators=(",", ":"))


async def fetch_pin_raw(pin_input: str, client: PinterestClient) -> dict[str, Any]:
    """Fetch a pin and return the parsed raw JSON (no field mapping yet).

    Raises BlockedError on persistent block, PinUrlError on bad input.
    """
    pin_id = await resolve_pin_id(pin_input, client)
    source_url = f"/pin/{pin_id}/"
    data = build_pin_data_param(pin_id)

    resp = await client.get_resource(
        PIN_RESOURCE_PATH, source_url=source_url, data=data
    )

    if resp.status_code != 200:
        raise BlockedError(f"Pinterest returned HTTP {resp.status_code}")

    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise BlockedError(
            "Pinterest returned a non-JSON response (likely a block page)."
        ) from exc

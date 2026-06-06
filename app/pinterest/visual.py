"""Reverse image / visual search ("flashlight") — ISOLATED, low-risk.

Verified against Pinterest's real network traffic: visual search runs as a
GET by an EXISTING pin — there is NO image upload involved. This makes it as
low-risk as pin lookup (a read, looks like normal browsing) and means the
throwaway account is never asked to upload anything.

Endpoint (verified):
  GET /resource/ApiResource/get/
    source_url = /pin/<pin_id>/visual-search/?x=..&y=..&w=..&h=..&surfaceType=flashlight
    data = {"options":{"url":"/v3/visual_search/flashlight/pin/<pin_id>/",
                       "data":{"x":..,"y":..,"w":..,"h":..,
                               "request_source":9,"crop_source":5},
                       "bookmarks":[]},"context":{}}

Isolated + graceful: every failure degrades to VisualSearchUnavailable; nothing
here crashes the app, and pin lookup is entirely unaffected.
"""
from __future__ import annotations

import json
from typing import Any

from app.pinterest.client import BlockedError, PinterestClient
from app.pinterest.models import ImageMatch

API_RESOURCE_PATH = "/resource/ApiResource/get/"
MAX_RESULTS = 24


class VisualSearchUnavailable(Exception):
    """Visual search could not be completed; caller shows a friendly message."""


def _build_request(pin_id: str) -> tuple[str, str]:
    """Build (source_url, data) for a full-image flashlight search of a pin."""
    # x/y/w/h are normalised crop coords (0..1); full image = whole pin.
    crop = {
        "x": 0.0,
        "y": 0.0,
        "w": 1.0,
        "h": 1.0,
        "request_source": 9,  # magic constants observed in live traffic
        "crop_source": 5,
    }
    source_url = (
        f"/pin/{pin_id}/visual-search/?x=0&y=0&w=1&h=1&surfaceType=flashlight"
    )
    options = {
        "url": f"/v3/visual_search/flashlight/pin/{pin_id}/",
        "data": crop,
        "bookmarks": [],
    }
    data = json.dumps({"options": options, "context": {}}, separators=(",", ":"))
    return source_url, data


def _parse_matches(raw: dict[str, Any]) -> list[ImageMatch]:
    """Map a visual-search response to ImageMatch list (defensive)."""
    data = raw.get("resource_response", {})
    if isinstance(data, dict):
        data = data.get("data", [])
    # Some responses wrap results under {"results": [...]}.
    if isinstance(data, dict):
        data = data.get("results") or data.get("pins") or []
    results = data if isinstance(data, list) else []

    matches: list[ImageMatch] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        # Skip non-pin modules (ads, separators, etc.).
        if item.get("type") not in (None, "pin"):
            continue
        pin_id = item.get("id")
        if not pin_id:
            continue
        images = item.get("images", {})
        image = None
        if isinstance(images, dict):
            for size in ("236x", "474x", "orig"):
                node = images.get(size)
                if isinstance(node, dict) and node.get("url"):
                    image = node["url"]
                    break
        board = item.get("board")
        matches.append(
            ImageMatch(
                pin_id=str(pin_id),
                url=f"https://www.pinterest.com/pin/{pin_id}/",
                image=image,
                title=(item.get("title") or item.get("grid_title") or None),
                board_name=(board.get("name") if isinstance(board, dict) else None),
                match_source="flashlight",
            )
        )
        if len(matches) >= MAX_RESULTS:
            break
    return matches


async def visual_search_by_pin(
    pin_id: str,
    client: PinterestClient,
    *,
    anonymous: bool = True,
) -> list[ImageMatch]:
    """Find pins visually similar to an existing pin. Anonymous-first.

    Raises VisualSearchUnavailable on block/failure (the API layer turns this
    into a friendly message). Never crashes.
    """
    source_url, data = _build_request(pin_id)
    try:
        resp = await client.get_resource(
            API_RESOURCE_PATH, source_url=source_url, data=data, anonymous=anonymous
        )
    except (BlockedError, Exception) as exc:  # noqa: BLE001
        raise VisualSearchUnavailable(f"Visual search request failed: {exc}") from exc

    if resp.status_code != 200:
        raise VisualSearchUnavailable(
            f"Visual search returned HTTP {resp.status_code}."
        )
    try:
        raw = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise VisualSearchUnavailable("Visual search returned non-JSON.") from exc

    return _parse_matches(raw)

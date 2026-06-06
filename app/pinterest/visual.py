"""Reverse image search (visual search) — HIGHEST-RISK feature, fully ISOLATED.

Design constraints (deliberately conservative):
  - ANONYMOUS-FIRST: uploads/searches use the cookie-free client by default, so
    the throwaway account is never involved unless explicitly enabled later.
  - Isolated: a failure here NEVER affects pin lookup. Every path degrades to a
    clear "unavailable" message; nothing raises out of `visual_search`.
  - Upload is a write-like action, so it is retry-light and extra-throttled.

The endpoint paths/field names below are HYPOTHESES to verify against live
network traffic (via the probe), exactly as we did for annotations. Until
verified, `visual_search` returns a graceful "unavailable".
"""
from __future__ import annotations

import json
from typing import Any

from app.pinterest.client import BlockedError, PinterestClient
from app.pinterest.models import ImageMatch

# Hypothesised endpoints (verify with the probe before relying on them):
#   1. Upload an image -> returns an image URL / signature
#   2. Query a visual-search resource with that URL/signature -> similar pins
UPLOAD_PATH = "/_ngjs/resource/ImageResource/create/"  # hypothesis
VISUAL_SEARCH_PATH = "/resource/VisualLiveSearchResource/get/"  # hypothesis

MAX_RESULTS = 24


class VisualSearchUnavailable(Exception):
    """Visual search could not be completed; caller shows a friendly message."""


async def upload_image(
    image_bytes: bytes,
    content_type: str,
    client: PinterestClient,
    *,
    anonymous: bool = True,
) -> str | None:
    """Upload an image to Pinterest; return an image URL/signature, or None.

    Best-effort and isolated: returns None on any failure rather than raising.
    """
    files = {"img": ("upload.jpg", image_bytes, content_type or "image/jpeg")}
    try:
        resp = await client.post_multipart(
            UPLOAD_PATH, files=files, anonymous=anonymous
        )
    except (BlockedError, Exception):  # noqa: BLE001 - never propagate
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    # The upload response shape is a hypothesis; try the likely locations.
    for path in (
        ("resource_response", "data", "url"),
        ("resource_response", "data", "image_url"),
        ("resource_response", "data"),
    ):
        cur: Any = data
        for key in path:
            if isinstance(cur, dict):
                cur = cur.get(key)
            else:
                cur = None
                break
        if isinstance(cur, str) and cur.startswith("http"):
            return cur
    return None


def _parse_matches(raw: dict[str, Any]) -> list[ImageMatch]:
    """Map a visual-search response to ImageMatch list (defensive)."""
    matches: list[ImageMatch] = []
    data = raw.get("resource_response", {})
    if isinstance(data, dict):
        data = data.get("data", [])
    results = data if isinstance(data, list) else []
    for item in results:
        if not isinstance(item, dict):
            continue
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
                match_source="visual_search",
            )
        )
        if len(matches) >= MAX_RESULTS:
            break
    return matches


async def visual_search(
    image_bytes: bytes,
    content_type: str,
    client: PinterestClient,
    *,
    anonymous: bool = True,
) -> list[ImageMatch]:
    """Full flow: upload -> query similar pins. Returns [] gracefully on failure.

    Raises VisualSearchUnavailable only to let the API layer show a specific
    message; it never crashes the app.
    """
    image_url = await upload_image(
        image_bytes, content_type, client, anonymous=anonymous
    )
    if not image_url:
        raise VisualSearchUnavailable("Image upload failed or was blocked.")

    options = {
        "image_url": image_url,
        "crop": {"x": 0, "y": 0, "w": 1, "h": 1},
        "page_size": MAX_RESULTS,
    }
    data = json.dumps({"options": options, "context": {}}, separators=(",", ":"))
    try:
        resp = await client.post_resource(
            VISUAL_SEARCH_PATH, source_url="/", data=data, anonymous=anonymous
        )
    except (BlockedError, Exception) as exc:  # noqa: BLE001
        raise VisualSearchUnavailable(f"Visual search query failed: {exc}") from exc
    if resp.status_code != 200:
        raise VisualSearchUnavailable(
            f"Visual search returned HTTP {resp.status_code}."
        )
    try:
        raw = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise VisualSearchUnavailable("Visual search returned non-JSON.") from exc

    return _parse_matches(raw)

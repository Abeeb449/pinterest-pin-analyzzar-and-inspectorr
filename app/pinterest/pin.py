"""Pin-detail fetch + parse.

resolve a pin URL to an ID, build the PinResource request, fetch it
anonymously, and map the raw JSON -> PinData defensively (every Pinterest field
may be missing, so nothing here may raise on absence).
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.pinterest.client import BlockedError, PinterestClient
from app.pinterest.models import Comment, PinData

PIN_RESOURCE_PATH = "/resource/PinResource/get/"
OEMBED_URL = "https://www.pinterest.com/oembed.json"

# Matches /pin/<digits>/ in a full or partial Pinterest URL.
_PIN_ID_RE = re.compile(r"/pin/(\d+)")
_BARE_ID_RE = re.compile(r"^\d+$")

# A short body that is exactly a block marker (Pinterest edge returns "BLOCKED").
_BLOCK_MARKERS = ("BLOCKED", "Access Denied", "Request blocked")


class PinUrlError(ValueError):
    """The provided string is not a recognizable Pinterest pin URL/ID."""


def _looks_blocked(status_code: int, body: str) -> bool:
    """Heuristic: did we hit an edge block rather than a real page?"""
    if status_code in (403, 429):
        return True
    head = (body or "").strip()[:64]
    return any(marker.lower() in head.lower() for marker in _BLOCK_MARKERS)


async def resolve_pin_id(raw: str, client: PinterestClient) -> str:
    """Extract a numeric pin id from a URL, short link, or bare id.

    Raises BlockedError if the short-link resolution is blocked by the edge,
    and PinUrlError only when the input genuinely has no pin id.
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

        # Couldn't find an id. Distinguish "blocked" from "genuinely bad link"
        # so the UI can show an accurate message.
        if _looks_blocked(resp.status_code, body):
            raise BlockedError(
                "Pinterest blocked the short-link lookup "
                f"(HTTP {resp.status_code}). Try the full /pin/<id>/ URL."
            )

    raise PinUrlError("Could not find a pin id in that input.")


def build_pin_data_param(pin_id: str) -> str:
    """Build the `data` query param for the PinResource request."""
    payload = {
        "options": {"id": pin_id, "field_set_key": "detailed"},
        "context": {},
    }
    return json.dumps(payload, separators=(",", ":"))


async def fetch_pin_raw(pin_input: str, client: PinterestClient) -> dict[str, Any]:
    """Fetch a pin and return the parsed raw JSON (no field mapping).

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


async def fetch_pin_raw_debug(
    pin_id: str, client: PinterestClient
) -> tuple[dict[str, Any], str]:
    """DEBUG: return the raw pin object from whichever rich source responds.

    Tries the JSON API first; if blocked, pulls the pin object out of the
    embedded page state. Returns (raw_pin_object, source_label) so we can read
    the TRUE field paths for comments/annotations on a real response.
    """
    # 1. JSON API
    try:
        raw = await fetch_pin_raw(pin_id, client)
        pin_obj = extract_pin_object(raw)
        if pin_obj:
            return pin_obj, "json_api"
        return raw, "json_api_envelope"
    except BlockedError:
        pass

    # 2. Embedded page state
    pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
    resp = await client.get_with_retry(pin_url, accept="text/html")
    html = resp.text or ""
    for m in _PWS_DATA_RE.finditer(html):
        try:
            blob = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        pin_obj = _find_pin_in_blob(blob, pin_id)
        if pin_obj is not None:
            return pin_obj, "embedded_state"
    raise BlockedError("Could not obtain a raw pin object from any rich source.")


# --------------------------------------------------------------------------- #
# Raw -> PinData mapping (defensive)
# --------------------------------------------------------------------------- #
#
# Field paths below are HYPOTHESES from the brief, verified opportunistically
# against the live response. Each lookup tolerates absence and records a note
# rather than raising, so a partial/blocked response still yields a PinData.


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """Safe nested dict getter: _get(obj, 'a', 'b') == obj['a']['b'] or default."""
    cur = d
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _first(*values: Any, default: Any = None) -> Any:
    """Return the first non-empty value."""
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return default


def _to_int(value: Any) -> int | None:
    """Coerce to int only if it's a real number; never fabricate."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def extract_pin_object(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the pin object out of the resource envelope."""
    data = _get(raw, "resource_response", "data")
    # Some responses nest the pin under data, others return it directly.
    if isinstance(data, dict):
        # detailed pin fetch returns the pin dict directly under data
        if "id" in data or "type" in data:
            return data
        # occasionally wrapped again
        inner = data.get("data") if isinstance(data.get("data"), dict) else None
        if inner:
            return inner
    return None


def _map_board(pin: dict[str, Any], notes: list[str]) -> tuple[str | None, str | None]:
    board = _get(pin, "board")
    if not isinstance(board, dict):
        notes.append("board: unavailable")
        return None, None
    name = _first(board.get("name"))
    url_path = _first(board.get("url"))
    url = ("https://www.pinterest.com" + url_path) if url_path and url_path.startswith("/") else url_path
    return name, url


def _map_pinner(pin: dict[str, Any], notes: list[str]) -> tuple[str | None, str | None, str | None]:
    # Brief hint: .native_creator / .pinner
    creator = _first(_get(pin, "native_creator"), _get(pin, "pinner"))
    if not isinstance(creator, dict):
        notes.append("pinner: unavailable")
        return None, None, None
    name = _first(creator.get("full_name"), creator.get("username"))
    username = _first(creator.get("username"))
    url = ("https://www.pinterest.com/" + username + "/") if username else None
    return name, username, url


def _map_saves(pin: dict[str, Any], notes: list[str]) -> int | None:
    # Brief hint: aggregated_pin_data.aggregated_stats.saves (often absent).
    saves = _to_int(_get(pin, "aggregated_pin_data", "aggregated_stats", "saves"))
    if saves is None:
        # fall back to a top-level repin/save count if Pinterest exposes one
        saves = _to_int(_first(pin.get("repin_count"), pin.get("save_count")))
    if saves is None:
        notes.append("saves: unavailable")  # never estimate
    return saves


def _map_reactions(pin: dict[str, Any]) -> int | None:
    # Only if Pinterest actually reports reactions. No fake likes.
    reactions = _to_int(_get(pin, "reaction_counts"))
    if reactions is not None:
        return reactions
    rc = _get(pin, "reaction_counts")
    if isinstance(rc, dict):
        total = sum(v for v in rc.values() if isinstance(v, int))
        return total or None
    return _to_int(pin.get("total_reaction_count"))


def _collect_annotation_strings(source: Any, out: list[str]) -> None:
    """Pull keyword strings out of whatever shape an annotation source takes."""
    if isinstance(source, dict):
        # pin_join.annotations is commonly { "keyword": "/search/url", ... }
        for key, val in source.items():
            if isinstance(key, str) and key and not key.isdigit():
                out.append(key)
            elif isinstance(val, str) and val:
                out.append(val)
    elif isinstance(source, list):
        for item in source:
            if isinstance(item, dict):
                name = _first(
                    item.get("name"), item.get("keyword"),
                    item.get("label"), item.get("term"), item.get("text"),
                )
                if name:
                    out.append(str(name))
            elif isinstance(item, (str, int)):
                out.append(str(item))


def _map_annotations(pin: dict[str, Any]) -> list[str]:
    out: list[str] = []
    # Known/historical annotation locations (verified against real payloads).
    for source in (
        _get(pin, "pin_join", "visual_annotation"),
        _get(pin, "pin_join", "annotations"),
        _get(pin, "pin_join", "annotations_with_links"),
        pin.get("visual_objects"),
        pin.get("visual_annotation"),
        pin.get("pin_note_annotations"),
        _get(pin, "auto_alt_text"),  # sometimes a single descriptive string
    ):
        _collect_annotation_strings(source, out)
    # de-dup, preserve order
    seen: set[str] = set()
    deduped = []
    for a in out:
        a = a.strip()
        if a and a not in seen:
            seen.add(a)
            deduped.append(a)
    return deduped


def _map_comments(pin: dict[str, Any]) -> tuple[int | None, list[Comment]]:
    # Comment COUNT lives in several spots depending on the surface.
    count = _to_int(
        _first(
            pin.get("comment_count"),
            _get(pin, "aggregated_pin_data", "comment_count"),
            _get(pin, "aggregated_pin_data", "aggregated_stats", "comments"),
        )
    )
    comments: list[Comment] = []
    # The actual comment OBJECTS are usually loaded by a separate request and
    # are absent from the pin object itself; map any that happen to be inline.
    raw_comments = _first(
        _get(pin, "comments", "data"),
        pin.get("comments") if isinstance(pin.get("comments"), list) else None,
        default=[],
    )
    if isinstance(raw_comments, list):
        for c in raw_comments:
            if not isinstance(c, dict):
                continue
            comments.append(
                Comment(
                    author=_first(_get(c, "user", "full_name"), _get(c, "user", "username")),
                    text=_first(c.get("text"), c.get("comment_text"), c.get("details")),
                    created_at=_first(c.get("created_at")),
                )
            )
    return count, comments


def parse_pin_data(raw: dict[str, Any], pin_id_hint: str | None = None) -> PinData:
    """Map a raw PinResource response to PinData. Never raises on missing fields."""
    notes: list[str] = []
    pin = extract_pin_object(raw)

    if pin is None:
        # surface any API-level error message if present
        err = _get(raw, "resource_response", "error")
        if err:
            notes.append(f"pinterest error: {err}")
        notes.append("pin object not found in response")
        return PinData(pin_id=pin_id_hint or "unknown", notes=notes)

    pin_id = _first(pin.get("id"), pin_id_hint, default="unknown")
    url = f"https://www.pinterest.com/pin/{pin_id}/" if pin_id != "unknown" else None

    title = _first(pin.get("title"), pin.get("grid_title"))
    description = _first(pin.get("description"), pin.get("description_html"))

    image = _first(
        _get(pin, "images", "orig", "url"),
        _get(pin, "images", "736x", "url"),
        _get(pin, "images", "474x", "url"),
        _get(pin, "image_large_url"),
    )

    board_name, board_url = _map_board(pin, notes)
    pinner_name, pinner_username, pinner_url = _map_pinner(pin, notes)
    saves = _map_saves(pin, notes)
    reactions = _map_reactions(pin)
    comment_count, comments = _map_comments(pin)
    annotations = _map_annotations(pin)

    created_at = _first(pin.get("created_at"))

    return PinData(
        pin_id=str(pin_id),
        url=url,
        title=title,
        description=description,
        image=image,
        board_name=board_name,
        board_url=board_url,
        pinner_name=pinner_name,
        pinner_username=pinner_username,
        pinner_url=pinner_url,
        created_at=created_at,
        saves=saves,
        reactions=reactions,
        comment_count=comment_count,
        comments=comments,
        annotations=annotations,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Fallback fetchers (used when the bot-protected JSON API returns 403)
# --------------------------------------------------------------------------- #
#
# The internal PinResource API is the most aggressively bot-blocked surface.
# These public/HTML surfaces are frequently reachable when it is not. They
# return LESS data (no saves/comments/annotations), but a partial result beats
# a hard block. Each returns a PinData or None (so the caller can keep trying).

# JSON embedded in the pin's HTML page (Next.js / relay state, JSON-LD, OG tags)
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)
_OG_RE = {
    "image": re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)'),
    "title": re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)'),
    "description": re.compile(
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)'
    ),
}


async def fetch_pin_via_oembed(pin_id: str, client: PinterestClient) -> PinData | None:
    """Fallback 1: Pinterest's public oEmbed endpoint.

    Gives title, author (pinner) name + url, and a thumbnail. No saves/comments.
    """
    pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
    url = f"{OEMBED_URL}?url={pin_url}"
    try:
        resp = await client.get_with_retry(url, accept="application/json")
    except BlockedError:
        return None
    if resp.status_code != 200:
        return None
    try:
        d = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None

    notes = ["source: oEmbed fallback (JSON API was blocked; saves/comments/annotations unavailable here)"]
    author = _first(d.get("author_name"))
    author_url = _first(d.get("author_url"))
    return PinData(
        pin_id=str(pin_id),
        url=pin_url,
        title=_first(d.get("title")),
        image=_first(d.get("thumbnail_url")),
        pinner_name=author,
        pinner_url=author_url,
        notes=notes,
    )


# Pinterest embeds its full app state as JSON in a <script> tag on the pin page.
# When authenticated, this blob carries the SAME rich pin object as the JSON API
# (saves, comments, annotations) — and the HTML page is far less bot-blocked
# than the /resource/ API path.
_PWS_DATA_RE = re.compile(
    r'<script[^>]+id=["\'](?:__PWS_DATA__|initial-state|__PWS_INITIAL_PROPS__)["\']'
    r'[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _find_pin_in_blob(obj: Any, pin_id: str, depth: int = 0) -> dict[str, Any] | None:
    """Recursively search the embedded app-state JSON for THE pin object.

    A match is a dict whose id equals pin_id and that looks like a pin (has
    pin-ish keys). Bounded depth so a huge blob can't blow the stack.
    """
    if depth > 12:
        return None
    if isinstance(obj, dict):
        oid = obj.get("id")
        if (
            str(oid) == str(pin_id)
            and any(k in obj for k in ("aggregated_pin_data", "board", "pinner",
                                       "native_creator", "story_pin_data", "images"))
        ):
            return obj
        for v in obj.values():
            found = _find_pin_in_blob(v, pin_id, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_pin_in_blob(v, pin_id, depth + 1)
            if found is not None:
                return found
    return None


async def fetch_pin_via_embedded_state(
    pin_id: str, client: PinterestClient
) -> PinData | None:
    """Strategy 2 (rich, needs auth): extract the embedded app-state JSON from
    the pin's HTML page and map it exactly like the JSON API response.

    When the session cookie is set, this returns saves/comments/annotations
    even though the /resource/ API path is blocked.
    """
    pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
    try:
        resp = await client.get_with_retry(pin_url, accept="text/html")
    except BlockedError:
        return None
    if resp.status_code != 200:
        return None
    html = resp.text or ""
    if not html:
        return None

    for m in _PWS_DATA_RE.finditer(html):
        chunk = m.group(1).strip()
        try:
            blob = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        pin_obj = _find_pin_in_blob(blob, pin_id)
        if pin_obj is not None:
            # Reuse the exact same defensive mapper as the JSON API path.
            pin = parse_pin_data(
                {"resource_response": {"data": pin_obj}}, pin_id_hint=pin_id
            )
            # Only accept if it actually yielded the rich bits worth having.
            if pin.title or pin.image or pin.board_name or pin.saves is not None:
                pin.notes.insert(
                    0,
                    "source: authenticated page state (full data via embedded JSON)",
                )
                return pin
    return None


async def fetch_pin_via_html(pin_id: str, client: PinterestClient) -> PinData | None:
    """Fallback 3: scrape the pin's public HTML page (OG tags + JSON-LD).

    Gives title, description, image, and sometimes pinner. No saves/comments.
    """
    pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
    try:
        resp = await client.get_with_retry(pin_url, accept="text/html")
    except BlockedError:
        return None
    if resp.status_code != 200:
        return None
    html = resp.text or ""
    if not html:
        return None

    title = description = image = pinner_name = None

    # Open Graph meta tags (most reliable, present on the public page)
    for key, rx in _OG_RE.items():
        m = rx.search(html)
        if m:
            val = m.group(1)
            if key == "title":
                title = val
            elif key == "description":
                description = val
            elif key == "image":
                image = val

    # JSON-LD often carries author / creator
    for m in _JSONLD_RE.finditer(html):
        try:
            ld = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            continue
        blocks = ld if isinstance(ld, list) else [ld]
        for b in blocks:
            if not isinstance(b, dict):
                continue
            author = b.get("author")
            if isinstance(author, dict):
                pinner_name = pinner_name or _first(author.get("name"))
            title = title or _first(b.get("headline"), b.get("name"))
            description = description or _first(b.get("description"))

    if not any((title, description, image)):
        return None  # page returned but had nothing useful -> let caller decide

    return PinData(
        pin_id=str(pin_id),
        url=pin_url,
        title=title,
        description=description,
        image=image,
        pinner_name=pinner_name,
        notes=["source: HTML page fallback (JSON API was blocked; saves/comments/annotations unavailable here)"],
    )


async def fetch_pin(pin_input: str, client: PinterestClient) -> PinData:
    """High-level: resolve -> fetch -> parse into PinData.

    Strategy ladder (richest first; falls through on block/empty):
      1. Internal PinResource JSON API — richest, but the most bot-blocked
         surface (often 403s on datacenter IPs even when authenticated).
      2. Authenticated embedded page state — the pin HTML page carries the
         SAME rich object (saves/comments/annotations) in a JSON <script>;
         far less blocked than the API path. This is the main win with a cookie.
      3. oEmbed endpoint — thin (title, pinner, thumbnail).
      4. Public HTML scrape — thin (title, description, image).

    The first strategy that yields usable data wins. Only if ALL are blocked do
    we raise BlockedError so the UI shows the graceful "blocked" message.
    """
    pin_id = await resolve_pin_id(pin_input, client)

    # Strategy 1: the rich JSON API.
    primary_error: Exception | None = None
    try:
        raw = await fetch_pin_raw(pin_id, client)
        pin = parse_pin_data(raw, pin_id_hint=pin_id)
        # Treat "pin object not found" as a soft failure worth falling back on.
        if pin.title or pin.image or pin.board_name or pin.pinner_name:
            return pin
        primary_error = BlockedError("JSON API returned no usable pin object")
    except BlockedError as exc:
        primary_error = exc

    # Strategies 2-4: embedded rich state first (best with a cookie), then the
    # thin public surfaces as a last resort.
    for fallback in (
        fetch_pin_via_embedded_state,
        fetch_pin_via_oembed,
        fetch_pin_via_html,
    ):
        try:
            result = await fallback(pin_id, client)
        except Exception:  # noqa: BLE001 - a failing fallback must not crash
            result = None
        if result is not None:
            return result

    # Everything was blocked.
    raise BlockedError(
        "All fetch methods were blocked (JSON API, embedded state, oEmbed, "
        f"and HTML page). Last API error: {primary_error}"
    )

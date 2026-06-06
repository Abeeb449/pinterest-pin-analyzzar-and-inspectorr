"""FastAPI application entry point.

Serves the static frontend from the same origin as the API (no CORS), and the
app's password gate. Pin lookup and visual search are layered on in later steps.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import auth, config
from app.pinterest import pin as pin_mod
from app.pinterest import visual as visual_mod
from app.pinterest.client import BlockedError, close_client, get_client


def require_debug() -> None:
    """Gate debug endpoints: 404 (invisible) unless DEBUG_ENDPOINTS is set."""
    if not config.DEBUG_ENDPOINTS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pinterest Pin Intelligence Tool", docs_url=None, redoc_url=None)

# Serve CSS/JS/assets under /static (same origin as the API).
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginRequest(BaseModel):
    password: str


class PinLookupRequest(BaseModel):
    url: str


class ResourceProbeRequest(BaseModel):
    resource: str
    options: dict = {}
    source_url: str = "/"
    anonymous: bool = False


@app.on_event("shutdown")
async def _shutdown() -> None:
    """Close the shared Pinterest httpx client on app shutdown."""
    await close_client()


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Public, unauthenticated health check (handy for uptime pings)."""
    return JSONResponse(
        {
            "status": "ok",
            "mode": "authenticated" if config.using_pinterest_cookie() else "anonymous",
        }
    )


@app.post("/api/login")
def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Check the password and, on success, set the signed session cookie."""
    if not auth.verify_password(body.password):
        return JSONResponse({"ok": False, "error": "Incorrect password."}, status_code=401)
    response = JSONResponse({"ok": True})
    auth.issue_session(response, secure=auth.is_secure_request(request))
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    """Clear the session cookie."""
    response = JSONResponse({"ok": True})
    auth.clear_session(response)
    return response


@app.get("/api/session", dependencies=[Depends(auth.require_auth)])
def session_info() -> JSONResponse:
    """Authenticated probe: the frontend uses this to decide login vs app view."""
    return JSONResponse(
        {
            "authenticated": True,
            "mode": "authenticated" if config.using_pinterest_cookie() else "anonymous",
        }
    )


@app.post("/api/pin", dependencies=[Depends(auth.require_auth)])
async def lookup_pin(body: PinLookupRequest) -> JSONResponse:
    """Look up a pin's metadata.

    Always returns 200 with a structured payload so the frontend can render
    partial data + a status message instead of treating blocks as crashes.
    """
    raw_input = (body.url or "").strip()
    if not raw_input:
        return JSONResponse(
            {"ok": False, "status": "error", "message": "Please enter a pin URL."}
        )

    client = get_client()
    try:
        pin_data = await pin_mod.fetch_pin(raw_input, client)
    except pin_mod.PinUrlError as exc:
        return JSONResponse(
            {"ok": False, "status": "bad_input", "message": str(exc)}
        )
    except BlockedError as exc:
        return JSONResponse(
            {
                "ok": False,
                "status": "blocked",
                "message": (
                    "Pinterest blocked this request (common on free shared-IP "
                    "hosting). Try again shortly. " + str(exc)
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001 - never crash the endpoint
        return JSONResponse(
            {
                "ok": False,
                "status": "error",
                "message": f"Unexpected error: {exc}",
            }
        )

    # Partial success is still success; notes carry any missing-field info.
    return JSONResponse(
        {"ok": True, "status": "ok", "pin": pin_data.model_dump()}
    )


@app.post(
    "/api/pin/raw",
    dependencies=[Depends(require_debug), Depends(auth.require_auth)],
)
async def lookup_pin_raw(body: PinLookupRequest) -> JSONResponse:
    """DEBUG: return the raw, unmapped JSON Pinterest gives us for a pin.

    Used to confirm the TRUE field paths for comments/annotations on a real
    response. Auth-gated like everything else; returns whichever rich source
    actually responded (API or embedded page state).
    """
    raw_input = (body.url or "").strip()
    if not raw_input:
        return JSONResponse({"ok": False, "message": "Please enter a pin URL."})

    client = get_client()
    try:
        pin_id = await pin_mod.resolve_pin_id(raw_input, client)
        raw, source = await pin_mod.fetch_pin_raw_debug(pin_id, client)
    except pin_mod.PinUrlError as exc:
        return JSONResponse({"ok": False, "status": "bad_input", "message": str(exc)})
    except BlockedError as exc:
        return JSONResponse({"ok": False, "status": "blocked", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "status": "error", "message": str(exc)})

    return JSONResponse({"ok": True, "source": source, "pin_id": pin_id, "raw": raw})


@app.post(
    "/api/resource/probe",
    dependencies=[Depends(require_debug), Depends(auth.require_auth)],
)
async def probe_resource(body: ResourceProbeRequest) -> JSONResponse:
    """DEBUG: call an arbitrary Pinterest resource and return raw JSON.

    Used to discover the real comments/annotations endpoint shapes.
    """
    client = get_client()
    try:
        raw = await pin_mod.fetch_resource_raw(
            client, body.resource, body.options,
            source_url=body.source_url, anonymous=body.anonymous,
        )
    except BlockedError as exc:
        return JSONResponse({"ok": False, "status": "blocked", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "status": "error", "message": str(exc)})
    return JSONResponse({"ok": True, "raw": raw})


@app.post("/api/visual", dependencies=[Depends(auth.require_auth)])
async def visual_search_endpoint(image: UploadFile = File(...)) -> JSONResponse:
    """Reverse image search. ISOLATED + anonymous-first.

    Always returns 200 with a structured payload; on any failure it reports
    the feature as unavailable rather than crashing.
    """
    content_type = (image.content_type or "").lower()
    if not content_type.startswith("image/"):
        return JSONResponse(
            {"ok": False, "status": "bad_input", "message": "Please upload an image file."}
        )

    image_bytes = await image.read()
    # Guard against oversized uploads (10 MB cap).
    if len(image_bytes) > 10 * 1024 * 1024:
        return JSONResponse(
            {"ok": False, "status": "bad_input", "message": "Image too large (max 10 MB)."}
        )
    if not image_bytes:
        return JSONResponse(
            {"ok": False, "status": "bad_input", "message": "Empty image."}
        )

    client = get_client()
    try:
        # Anonymous-first: never touches the throwaway account.
        matches = await visual_mod.visual_search(
            image_bytes, content_type, client, anonymous=True
        )
    except visual_mod.VisualSearchUnavailable as exc:
        return JSONResponse(
            {
                "ok": False,
                "status": "unavailable",
                "message": (
                    "Reverse image search is currently unavailable "
                    "(Pinterest blocked it or changed its internals). "
                    + str(exc)
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001 - never crash
        return JSONResponse(
            {"ok": False, "status": "error", "message": f"Unexpected error: {exc}"}
        )

    return JSONResponse(
        {"ok": True, "status": "ok", "matches": [m.model_dump() for m in matches]}
    )


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page frontend."""
    return FileResponse(STATIC_DIR / "index.html")

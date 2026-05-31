"""FastAPI application entry point.

Serves the static frontend from the same origin as the API (no CORS), and the
app's password gate. Pin lookup and visual search are layered on in later steps.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import auth, config

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pinterest Pin Intelligence Tool", docs_url=None, redoc_url=None)

# Serve CSS/JS/assets under /static (same origin as the API).
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginRequest(BaseModel):
    password: str


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


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page frontend."""
    return FileResponse(STATIC_DIR / "index.html")

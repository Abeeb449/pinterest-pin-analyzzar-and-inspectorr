"""FastAPI application entry point.

Phase 1, Step 1 scaffold: serves the static frontend from the same origin as
the (forthcoming) API, so there is no CORS between them. Auth, pin lookup, and
visual search are layered on in later steps.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Pinterest Pin Intelligence Tool", docs_url=None, redoc_url=None)

# Serve CSS/JS/assets under /static (same origin as the API).
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Lightweight health check (also handy for Render uptime checks)."""
    return JSONResponse(
        {
            "status": "ok",
            "mode": "authenticated" if config.using_pinterest_cookie() else "anonymous",
        }
    )


@app.get("/")
def index() -> FileResponse:
    """Serve the single-page frontend."""
    return FileResponse(STATIC_DIR / "index.html")

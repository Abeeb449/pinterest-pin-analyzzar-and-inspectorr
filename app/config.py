"""Central configuration, loaded from environment variables.

Local dev loads from a `.env` file (via python-dotenv); on Render the same
variables are set in the dashboard. The app MUST run with the Pinterest cookie
vars unset/empty — anonymous mode is the default, first-shipped behavior.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# Load .env for local development. On Render this is a no-op (no .env file),
# and real environment variables take precedence.
load_dotenv()


def _clean(value: str | None) -> str:
    """Treat None and whitespace-only values as empty string."""
    return (value or "").strip()


# --- Required ---
APP_PASSWORD: str = _clean(os.getenv("APP_PASSWORD"))

# Secret for signing the login session cookie. Fall back to a clearly-marked
# dev default so the app still boots locally, but warn loudly elsewhere.
SESSION_SECRET: str = _clean(os.getenv("SESSION_SECRET")) or "dev-insecure-session-secret-change-me"

# --- Optional Pinterest auth (Phase 2). Empty == anonymous mode. ---
PINTEREST_SESS_COOKIE: str = _clean(os.getenv("PINTEREST_SESS_COOKIE"))
PINTEREST_CSRF_TOKEN: str = _clean(os.getenv("PINTEREST_CSRF_TOKEN"))

# --- Tuning ---
try:
    PINTEREST_REQUEST_DELAY: float = float(_clean(os.getenv("PINTEREST_REQUEST_DELAY")) or "1.0")
except ValueError:
    PINTEREST_REQUEST_DELAY = 1.0


def using_pinterest_cookie() -> bool:
    """True when a Pinterest session cookie is configured (Phase 2)."""
    return bool(PINTEREST_SESS_COOKIE)

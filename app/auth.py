"""Password gate for the whole app.

This is the app's OWN lightweight auth — a personal gate so strangers can't use
the public deployment. It is unrelated to any Pinterest session cookie.

Flow:
  - POST /api/login with the password -> checked against APP_PASSWORD.
  - On success we set a signed, HttpOnly session cookie (signed with
    SESSION_SECRET via itsdangerous, with an expiry).
  - The `require_auth` dependency validates that cookie and rejects API calls
    that lack a valid one.
"""
from __future__ import annotations

import secrets

from fastapi import Cookie, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import config

COOKIE_NAME = "pi_session"
# How long a login lasts before re-auth is required (seconds).
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days
_SALT = "pi-login-v1"

_serializer = URLSafeTimedSerializer(config.SESSION_SECRET, salt=_SALT)


def verify_password(candidate: str) -> bool:
    """Constant-time check of a submitted password against APP_PASSWORD.

    If APP_PASSWORD is unset (misconfiguration), all logins are refused rather
    than silently admitting everyone.
    """
    expected = config.APP_PASSWORD
    if not expected:
        return False
    return secrets.compare_digest(candidate or "", expected)


def issue_session(response: Response, *, secure: bool) -> None:
    """Sign a session token and attach it as an HttpOnly cookie."""
    token = _serializer.dumps({"auth": True})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    """Remove the session cookie (logout)."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


def _cookie_is_valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return bool(isinstance(data, dict) and data.get("auth") is True)


def require_auth(pi_session: str | None = Cookie(default=None)) -> None:
    """FastAPI dependency: raise 401 unless a valid session cookie is present."""
    if not _cookie_is_valid(pi_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )


def is_secure_request(request: Request) -> bool:
    """Whether to mark the cookie Secure.

    True on HTTPS (Render serves HTTPS, possibly behind a proxy that sets
    X-Forwarded-Proto). False on plain-HTTP localhost so the cookie still works
    in local dev.
    """
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip() == "https"
    return request.url.scheme == "https"

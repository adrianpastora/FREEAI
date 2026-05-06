"""Shared helpers used by multiple routers.

Anything imported by more than one router lives here so the routers
themselves stay focused on request/response shape. Cross-cutting things
that belong with the orchestrator or providers are NOT here — see
``app.orchestrator`` and ``app.providers`` instead.
"""
from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException, Request
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from ..orchestrator import Orchestrator
from ..providers import ErrorKind, ProviderError


# ──────────────────────────── ProviderError → HTTP ────────────────────────────

_KIND_TO_STATUS = {
    ErrorKind.AUTH:         502,
    ErrorKind.RATE_LIMITED: 503,
    ErrorKind.CLIENT_ERROR: 400,
    ErrorKind.SERVER_ERROR: 502,
    ErrorKind.NETWORK:      504,
    ErrorKind.PARSING:      502,
    ErrorKind.UNKNOWN:      502,
}


def http_from_provider_error(e: ProviderError) -> HTTPException:
    return HTTPException(
        status_code=_KIND_TO_STATUS.get(e.kind, 502),
        detail={"provider": e.provider, "kind": e.kind.value, "message": e.message},
    )


def status_for_kind(kind: ErrorKind, default: int = 502) -> int:
    return _KIND_TO_STATUS.get(kind, default)


# ──────────────────────────── orchestrator dep ────────────────────────────


def get_orchestrator(request: Request) -> Orchestrator:
    """FastAPI dependency: pull the singleton orchestrator from app state."""
    return request.app.state.orchestrator


def require_user_id(request: Request) -> int:
    """FastAPI dependency: extract user_id bound by require_client.

    The /v1/* auth dependency populates ``request.state.user_id`` from the
    client API key, JWT, or admin-token bypass. Endpoints that need a
    user context use this to surface a single, consistent 400 when none
    of those auth paths matched.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        raise HTTPException(
            status_code=400,
            detail="no user context — authenticate with a client key bound to a user",
        )
    return user_id


# ──────────────────────────── setup helpers ────────────────────────────

# Placeholder pattern left by migration 0013 when an installation with a
# legacy admin token is upgraded but hasn't completed user migration yet.
# The hash is never a valid bcrypt output so no login can ever match it.
_PLACEHOLDER_PWD_HASH = "__placeholder_needs_migration__"

_SETUP_ADVISORY_LOCK_KEY = 98172354  # arbitrary but stable; pg_advisory_xact_lock


def is_placeholder(user) -> bool:
    return bool(user) and user.password_hash == _PLACEHOLDER_PWD_HASH


async def acquire_setup_lock(session: AsyncSession) -> None:
    """Serialize first-admin / setup flows against concurrent callers.

    Released automatically at transaction commit / rollback.
    """
    await session.execute(
        sa_text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _SETUP_ADVISORY_LOCK_KEY},
    )


# ──────────────────────────── login attempt rate limiter ────────────────────────────

LOGIN_ATTEMPT_WINDOW_SECONDS = 300  # 5 min
_LOGIN_ATTEMPT_MAX = 10
# keyed by (ip, username_lower) → deque[timestamp]
_login_attempts: dict[tuple[str, str], deque[float]] = {}


def client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For when we know a proxy is in front. Default to
    # the peer address to prevent header spoofing from bypassing the limit.
    peer = request.client.host if request.client else "unknown"
    return peer


def check_login_rate(ip: str, username: str) -> bool:
    """Return False when the caller has exceeded the window budget."""
    key = (ip, username.lower())
    now = time.time()
    dq = _login_attempts.setdefault(key, deque())
    while dq and dq[0] < now - LOGIN_ATTEMPT_WINDOW_SECONDS:
        dq.popleft()
    if len(dq) >= _LOGIN_ATTEMPT_MAX:
        return False
    dq.append(now)
    # Opportunistic cleanup of empty deques from other keys.
    if len(_login_attempts) > 10_000:
        stale = [k for k, v in _login_attempts.items() if not v]
        for k in stale:
            _login_attempts.pop(k, None)
    return True


def clear_login_attempts(ip: str, username: str) -> None:
    _login_attempts.pop((ip, username.lower()), None)


# ──────────────────────────── body-size budgets ────────────────────────────

# Max request body sizes. Audio uploads get a larger budget because they are
# binary; chat/JSON endpoints are bound tighter to blunt data-URI flooding.
MAX_BODY_BYTES_AUDIO = 25 * 1024 * 1024   # 25 MB
MAX_BODY_BYTES_DEFAULT = 10 * 1024 * 1024  # 10 MB


def body_limit_for(path: str) -> int:
    if path.startswith("/v1/audio/"):
        return MAX_BODY_BYTES_AUDIO
    return MAX_BODY_BYTES_DEFAULT


__all__ = [
    "acquire_setup_lock",
    "body_limit_for",
    "check_login_rate",
    "clear_login_attempts",
    "client_ip",
    "get_orchestrator",
    "http_from_provider_error",
    "is_placeholder",
    "require_user_id",
    "status_for_kind",
    "LOGIN_ATTEMPT_WINDOW_SECONDS",
    "MAX_BODY_BYTES_AUDIO",
    "MAX_BODY_BYTES_DEFAULT",
]

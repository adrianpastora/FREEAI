"""Inbound auth + per-client rate limiting (Postgres-backed).

Two layers:
  • require_admin — protects /api/* admin routes. Accepts (in order):
      FREEAI_ADMIN_TOKEN env, data/admin_token file, or bcrypt hash in
      app_config.admin_token_hash (set via first-run UI POST /api/setup/initial).
    Auto-generation of admin_token file was removed so a fresh install can use
    the setup wizard instead.
  • require_client — protects /v1/* client routes. Looks up the bearer key in
    the clients table and enforces per-client rpm via ClientRateRepository,
    which is backed by the `client_rate_events` table and the plpgsql
    function `freeai_try_reserve_client`. In bootstrap mode (no clients
    exist and FREEAI_REQUIRE_AUTH is false) the route is open.
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .crypto import verify_admin_token_hash
from .db.models import AppConfigRow
from .db.session import get_session
from .repositories import ClientRateRepository, ClientRepository
from .settings import get_settings


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


async def verify_admin_credentials(session: AsyncSession, presented: Optional[str]) -> bool:
    if not presented:
        return False
    settings = get_settings()
    if settings.admin_token:
        return secrets.compare_digest(presented, settings.admin_token)
    if settings.admin_token_path.exists():
        file_tok = settings.admin_token_path.read_text(encoding="utf-8").strip()
        return secrets.compare_digest(presented, file_tok)
    row = await session.get(AppConfigRow, 1)
    if row and row.admin_token_hash:
        return verify_admin_token_hash(presented, row.admin_token_hash)
    return False


# ──────────────── FastAPI dependencies ────────────────


async def require_client(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    session: AsyncSession = Depends(get_session),
):
    """Auth dependency for /v1/* — bootstrap-aware.

    Returns the matched Client DTO (or None in bootstrap mode). Raises 401 on
    missing/invalid keys and 429 on client rate limit breach.

    Also accepts the admin token (via X-Admin-Token header) as a bypass so the
    built-in playground can call /v1/* without needing a separate client key.
    """
    # Admin bypass — if a valid admin token is presented, skip client auth.
    if x_admin_token and await verify_admin_credentials(session, x_admin_token):
        return None

    settings = get_settings()
    client_repo = ClientRepository(session)
    has_clients = await client_repo.has_any()
    bootstrap = not has_clients and not settings.require_auth

    raw = _extract_bearer(authorization)

    if bootstrap and not raw:
        return None

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key (use 'Authorization: Bearer <key>')",
            headers={"WWW-Authenticate": "Bearer"},
        )

    client = await client_repo.find_by_raw_key(raw)
    if not client or not client.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    rate_repo = ClientRateRepository(session)
    granted = await rate_repo.try_acquire(client.key_hash, client.rpm_limit)
    if not granted:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"client rate limit exceeded ({client.rpm_limit} rpm)",
            headers={"Retry-After": "60"},
        )
    request.state.client = client
    return client


async def require_admin(
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> None:
    raw = _extract_bearer(authorization) or x_admin_token
    if not raw or not await verify_admin_credentials(session, raw):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin auth required")

"""Inbound auth + per-client rate limiting (Postgres-backed).

Three layers:
  • get_current_user / require_admin_user — JWT-based auth for multi-user.
  • require_admin — protects /api/* admin routes. Accepts JWT (role=admin)
      or legacy admin token (env, file, DB hash) for backwards compatibility.
  • require_client — protects /v1/* client routes. Looks up the bearer key in
    the clients table and enforces per-client rpm via ClientRateRepository.
    In bootstrap mode (no clients exist and FREEAI_REQUIRE_AUTH is false) the
    route is open.
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import CurrentUser, decode_access_token
from .crypto import verify_admin_token_hash
from .db.models import AppConfigRow
from .db.session import get_session
from .repositories import ClientRateRepository, ClientRepository
from .repositories.user_repo import UserRepository
from .settings import get_settings


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


async def verify_admin_credentials(session: AsyncSession, presented: Optional[str]) -> bool:
    """Legacy admin-token verification (env → file → DB hash)."""
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


def _try_jwt(authorization: Optional[str]) -> Optional[CurrentUser]:
    """Try to decode a JWT from the Authorization header. Returns None if not a JWT."""
    token = _extract_bearer(authorization)
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload:
        return None
    return CurrentUser(
        id=int(payload["sub"]),
        username=payload["username"],
        role=payload["role"],
    )


# ──────────────── FastAPI dependencies ────────────────


async def get_current_user(
    authorization: Optional[str] = Header(default=None),
) -> CurrentUser:
    """Extract and validate JWT. Raises 401 if missing or invalid."""
    user = _try_jwt(authorization)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def require_admin_user(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Require a valid JWT with role=admin."""
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return user


async def require_client(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    session: AsyncSession = Depends(get_session),
):
    """Auth dependency for /v1/* — bootstrap-aware.

    Returns the matched Client DTO (or None in bootstrap mode). Raises 401 on
    missing/invalid keys and 429 on client rate limit breach.

    Auth precedence:
      1. JWT (Authorization: Bearer <jwt>) — used by the built-in playground.
         Sets request.state.user_id from the JWT claims.
      2. Legacy admin token (X-Admin-Token header) — backwards compat.
         Resolves to the first admin user so user_id is always set.
      3. Client API key (Authorization: Bearer <api-key>) — external consumers.
    """
    # 1. Try JWT first — the playground sends the user's JWT here.
    jwt_user = _try_jwt(authorization)
    if jwt_user:
        request.state.user_id = jwt_user.id
        return None

    # 2. Legacy admin token bypass.
    if x_admin_token and await verify_admin_credentials(session, x_admin_token):
        user_repo = UserRepository(session)
        admin = await user_repo.find_first_admin()
        if admin:
            request.state.user_id = admin.id
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
    request.state.user_id = client.user_id
    return client


async def require_admin(
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Accepts JWT (role=admin) or legacy admin token."""
    # Try JWT first
    user = _try_jwt(authorization)
    if user and user.is_admin:
        return

    # Fall back to legacy admin token
    raw = _extract_bearer(authorization) or x_admin_token
    if raw and await verify_admin_credentials(session, raw):
        return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin auth required")

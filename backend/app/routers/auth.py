"""Auth endpoints — register / login / refresh / logout / migrate-token.

The first real user becomes admin and must present the bootstrap token.
Subsequent users require an admin JWT. The placeholder user inserted by
migration 0013 does not count as a real user.
"""
from __future__ import annotations

import time
from typing import Optional, Self

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete as sa_delete, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    CurrentUser,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from ..bootstrap import consume_bootstrap_token, verify_bootstrap_token
from ..db import get_session
from ..db.models import (
    AppConfigRow,
    ClientRow,
    ProviderStatsRow,
    RateEventRow,
    UsageEventRow,
    UserProviderRow,
    UserRow,
)
from ..logging_config import get_logger
from ..repositories import RefreshTokenRepository, UserRepository
from ..security import get_current_user, verify_admin_credentials
from ..settings import get_settings
from ._common import (
    LOGIN_ATTEMPT_WINDOW_SECONDS,
    acquire_setup_lock,
    check_login_rate,
    clear_login_attempts,
    client_ip,
    is_placeholder,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = get_logger("freeai.auth")


class RegisterBody(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=512)
    password_confirm: str = Field(..., min_length=8, max_length=512)

    @model_validator(mode="after")
    def _passwords_match(self) -> Self:
        if self.password != self.password_confirm:
            raise ValueError("passwords do not match")
        return self


class LoginBody(BaseModel):
    username: str
    password: str


class RefreshBody(BaseModel):
    refresh_token: str


class LogoutBody(BaseModel):
    refresh_token: str


class MigrateTokenBody(BaseModel):
    """One-time migration: verify legacy admin token, create admin user."""
    admin_token: str = Field(..., min_length=1)
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=512)
    password_confirm: str = Field(..., min_length=8, max_length=512)

    @model_validator(mode="after")
    def _passwords_match(self) -> Self:
        if self.password != self.password_confirm:
            raise ValueError("passwords do not match")
        return self


async def issue_tokens(
    user_id: int, username: str, role: str, session: AsyncSession,
) -> dict:
    """Create access + refresh tokens and persist the refresh hash.

    Public so the setup router can use it for the first-admin flow.
    """
    settings = get_settings()
    access = create_access_token(user_id, username, role)
    raw_refresh, refresh_hash = create_refresh_token()
    expires_at = time.time() + settings.jwt_refresh_expire_days * 86400

    refresh_repo = RefreshTokenRepository(session)
    await refresh_repo.store(user_id, refresh_hash, expires_at)

    return {
        "access_token": access,
        "refresh_token": raw_refresh,
        "token_type": "bearer",
        "expires_in": settings.jwt_access_expire_minutes * 60,
        "user": {"id": user_id, "username": username, "role": role},
    }


@router.get("/status")
async def auth_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Check if the system needs user migration or first-time registration."""
    user_repo = UserRepository(session)
    user_count = await user_repo.count()

    # Check if the only user is the migration placeholder
    if user_count == 1:
        placeholder = await user_repo.find_by_username("admin")
        if is_placeholder(placeholder):
            return {"status": "needs_migration", "user_count": 0}

    if user_count > 0:
        return {"status": "ready", "user_count": user_count}

    # Check if there's a legacy admin token to migrate from
    row = await session.get(AppConfigRow, 1)
    has_legacy = bool(row and row.admin_token_hash)
    settings = get_settings()
    has_legacy = has_legacy or bool(settings.admin_token) or settings.admin_token_path.exists()
    return {
        "status": "needs_migration" if has_legacy else "needs_setup",
        "user_count": 0,
    }


@router.post("/register", status_code=201)
async def register(
    body: RegisterBody,
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(default=None),
    x_bootstrap_token: Optional[str] = Header(default=None, alias="X-Bootstrap-Token"),
) -> dict:
    """Register a new user.

    The first real user becomes admin automatically and must present the
    bootstrap token printed to the server logs on first startup. Subsequent
    users require a valid admin JWT. The placeholder user created by migration
    0013 does not count as a real user.
    """
    # Serialize against concurrent registrations so two callers can't both
    # become the "first" admin by slipping past the count check in parallel.
    await acquire_setup_lock(session)

    user_repo = UserRepository(session)
    count = await user_repo.count()

    # Don't count the migration placeholder as a real user
    placeholder = await user_repo.find_by_username("admin")
    placeholder_present = is_placeholder(placeholder)
    real_count = count - (1 if placeholder_present else 0)

    if real_count == 0:
        # First admin — require bootstrap token to prevent drive-by takeover.
        if not verify_bootstrap_token(x_bootstrap_token):
            raise HTTPException(
                status_code=401,
                detail=(
                    "missing or invalid X-Bootstrap-Token header. The one-time "
                    "bootstrap token was printed to the server logs on startup "
                    "(data/.bootstrap_token)."
                ),
            )
    else:
        # Require admin JWT for creating additional users
        token = None
        if authorization:
            parts = authorization.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
        caller = None
        if token:
            payload = decode_access_token(token)
            if payload and payload.get("role") == "admin":
                caller = CurrentUser(
                    id=int(payload["sub"]),
                    username=payload["username"],
                    role=payload["role"],
                )
        if not caller:
            raise HTTPException(403, "only admins can register new users")
        if real_count >= 5:
            raise HTTPException(400, "maximum number of users reached (5)")

    # Check uniqueness
    existing = await user_repo.find_by_username(body.username)
    if existing and not (existing == placeholder and placeholder_present):
        raise HTTPException(409, f"username '{body.username}' is already taken")

    role = "admin" if real_count == 0 else "user"
    pwd_hash = hash_password(body.password)
    user_dto = await user_repo.create(body.username, pwd_hash, role=role)

    # If this is the first real admin and a placeholder exists,
    # transfer its providers, clients, and usage data to the new user
    if role == "admin" and placeholder_present and placeholder:
        for tbl, col in [
            (UserProviderRow, UserProviderRow.user_id),
            (ClientRow, ClientRow.user_id),
        ]:
            await session.execute(
                sa_update(tbl).where(col == placeholder.id).values(user_id=user_dto.id)
            )
        await session.execute(
            sa_update(RateEventRow).where(RateEventRow.user_id == placeholder.id).values(user_id=user_dto.id)
        )
        await session.execute(
            sa_update(UsageEventRow).where(UsageEventRow.user_id == placeholder.id).values(user_id=user_dto.id)
        )
        await session.execute(
            sa_delete(ProviderStatsRow).where(ProviderStatsRow.user_id == placeholder.id)
        )
        await user_repo.delete(placeholder.id)
        await session.flush()

    if role == "admin":
        consume_bootstrap_token()

    return await issue_tokens(user_dto.id, user_dto.username, user_dto.role, session)


@router.post("/login")
async def login(
    body: LoginBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    ip = client_ip(request)
    if not check_login_rate(ip, body.username):
        raise HTTPException(
            429,
            f"too many login attempts — try again in {LOGIN_ATTEMPT_WINDOW_SECONDS}s",
        )
    user_repo = UserRepository(session)
    user = await user_repo.find_by_username(body.username)
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid username or password")
    clear_login_attempts(ip, body.username)
    return await issue_tokens(user.id, user.username, user.role, session)


@router.post("/refresh")
async def refresh(body: RefreshBody, session: AsyncSession = Depends(get_session)) -> dict:
    refresh_repo = RefreshTokenRepository(session)
    token_hash = refresh_repo.hash_token(body.refresh_token)
    row = await refresh_repo.find_by_hash(token_hash)
    if not row or row.expires_at < time.time():
        raise HTTPException(401, "invalid or expired refresh token")

    user_repo = UserRepository(session)
    user = await user_repo.find_by_id(row.user_id)
    if not user:
        raise HTTPException(401, "user not found")

    # Rotate: delete old, issue new
    await refresh_repo.delete_by_hash(token_hash)
    return await issue_tokens(user.id, user.username, user.role, session)


@router.post("/logout")
async def logout(body: LogoutBody, session: AsyncSession = Depends(get_session)) -> dict:
    refresh_repo = RefreshTokenRepository(session)
    token_hash = refresh_repo.hash_token(body.refresh_token)
    await refresh_repo.delete_by_hash(token_hash)
    return {"ok": True}


@router.get("/me")
async def auth_me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/migrate-token", status_code=201)
async def migrate_token(
    body: MigrateTokenBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One-time migration: verify legacy admin token and create/update admin user.

    Used when upgrading from single-admin-token to multi-user. The caller
    proves they own the old token; the system creates a proper user account
    (or updates the placeholder created by migration 0013).
    """
    ip = client_ip(request)
    if not check_login_rate(ip, "__migrate_token__"):
        raise HTTPException(
            429,
            f"too many attempts — try again in {LOGIN_ATTEMPT_WINDOW_SECONDS}s",
        )

    # Serialize against concurrent calls so only one migration attempt wins.
    await acquire_setup_lock(session)

    user_repo = UserRepository(session)

    # Migration is only valid while the only real user (if any) is the
    # placeholder inserted by migration 0013. Once any real user exists —
    # even alongside the placeholder — this endpoint is permanently closed.
    placeholder = await user_repo.find_by_username("admin")
    placeholder_present = is_placeholder(placeholder)
    total_users = await user_repo.count()
    real_user_count = total_users - (1 if placeholder_present else 0)
    if real_user_count > 0:
        raise HTTPException(400, "migration already completed — users exist")

    if not await verify_admin_credentials(session, body.admin_token):
        raise HTTPException(401, "invalid admin token")
    clear_login_attempts(ip, "__migrate_token__")

    pwd_hash = hash_password(body.password)

    if placeholder_present:
        # Update the placeholder with real credentials
        await session.execute(
            sa_update(UserRow).where(UserRow.id == placeholder.id).values(
                username=body.username,
                password_hash=pwd_hash,
                updated_at=time.time(),
            )
        )
        await session.flush()
        user_dto = await user_repo.find_by_id(placeholder.id)
    else:
        existing = await user_repo.find_by_username(body.username)
        if existing:
            raise HTTPException(409, f"username '{body.username}' is already taken")
        user_dto = await user_repo.create(body.username, pwd_hash, role="admin")

    log.info("admin_migrated", username=body.username, user_id=user_dto.id)
    return await issue_tokens(user_dto.id, user_dto.username, user_dto.role, session)

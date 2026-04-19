"""FreeAI FastAPI entrypoint.

Exposes the OpenAI-compatible ``/v1`` surface (chat, embeddings, audio) and
the private ``/api`` control plane used by the frontend. Responsibilities
handled here: request/response shape, auth wiring, rate-limit middleware,
lifespan (migrations + bootstrap token), and background purge of event
tables. Routing, fallback, and provider selection live in ``orchestrator``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Self, Union

import httpx
import structlog
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from .crypto import hash_admin_token
from .db import create_engine_and_sessionmaker, dispose_engine, get_session
from .db.models import AppConfigRow
from .logging_config import configure_logging, get_logger
from .metrics import http_request_duration_seconds, http_requests_total, purge_rows_total, render_latest
from .orchestrator import Orchestrator
from .providers import PROVIDER_REGISTRY, ErrorKind, ProviderError
from .providers.known_models import KNOWN_MODELS, is_known, suggest_similar
from .repositories import (
    ClientRateRepository,
    ClientRepository,
    ConfigRepository,
    ProviderConfigDTO,
    RateRepository,
    RefreshTokenRepository,
    StrategyDTO,
    StrategyRepository,
    UsageRepository,
    UserRepository,
)
from .repositories.usage_repo import UsageEvent
from .repositories.config_repo import DEFAULT_PROVIDERS
from .repositories.user_provider_repo import UserProviderRepository
from .schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderStatus,
)
from .auth import (
    CurrentUser,
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from .bootstrap import (
    consume_bootstrap_token,
    ensure_bootstrap_token,
    verify_bootstrap_token,
)
from .security import get_current_user, require_admin, require_admin_user, require_client
from .settings import get_settings
from .strategy_dsl import ParseError, parse_definition
from .virtual_models import VIRTUAL_MODELS

configure_logging()
log = get_logger("freeai")


# ──────────────────────────── lifespan ────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine, sessionmaker = create_engine_and_sessionmaker()
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.orchestrator = Orchestrator()

    if settings.auto_migrate:
        await _run_migrations(settings.database_url)

    async with sessionmaker() as session:
        config_repo = ConfigRepository(session)
        strategy_repo = StrategyRepository(session)
        client_repo = ClientRepository(session)

        added = await config_repo.seed_defaults_if_empty()
        if added:
            log.info("seeded_default_providers", count=added)
        strat_added = await strategy_repo.seed_builtins_if_missing()
        if strat_added:
            log.info("seeded_builtin_strategies", count=strat_added)
        await session.commit()

        if not await client_repo.has_any():
            log.warning(
                "bootstrap_mode",
                message="no API clients configured — /v1/* is open. "
                        "Create a client via POST /api/clients before exposing.",
            )

        user_repo = UserRepository(session)
        user_count = await user_repo.count()
        placeholder = await user_repo.find_by_username("admin")
        is_placeholder = placeholder and placeholder.password_hash == "__placeholder_needs_migration__"
        real_users = user_count - (1 if is_placeholder else 0)
        cfg_row = await session.get(AppConfigRow, 1)
        has_admin_token = bool(cfg_row and cfg_row.admin_token_hash) or bool(settings.admin_token) or settings.admin_token_path.exists()
        needs_bootstrap = real_users == 0 and not has_admin_token
        new_token = ensure_bootstrap_token(needed=needs_bootstrap)
        if new_token:
            print(
                "\n"
                "============================================================\n"
                "  FreeAI bootstrap token (one-time, do not share):\n"
                f"    {new_token}\n"
                "  Send it in the X-Bootstrap-Token header when calling\n"
                "  POST /api/setup/initial or POST /api/auth/register.\n"
                "  Stored at data/.bootstrap_token until consumed.\n"
                "============================================================\n",
                flush=True,
            )

    log.info("freeai_ready", providers=len(PROVIDER_REGISTRY))
    purge_task = asyncio.create_task(_periodic_purge(sessionmaker))
    try:
        yield
    finally:
        purge_task.cancel()
        try:
            await purge_task
        except asyncio.CancelledError:
            pass
        await app.state.orchestrator.aclose()
        await dispose_engine(engine)


async def _run_migrations(database_url: str) -> None:
    log.info("running_migrations")
    proc = await asyncio.subprocess.create_subprocess_exec(
        sys.executable, "-m", "alembic", "upgrade", "head",
        cwd=str(Path(__file__).parent.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if stdout:
        # Keep the full output at debug only — verbose schema details don't
        # belong in production logs. If the run fails we print it below.
        for line in stdout.decode().splitlines():
            log.debug("alembic_stdout", line=line)
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        log.error(
            "migration_failed",
            returncode=proc.returncode,
            stdout=stdout.decode(errors="replace")[-2000:],
            stderr=err[-2000:],
        )
        raise RuntimeError(f"alembic upgrade failed (exit {proc.returncode})")
    log.info("migrations_done")


async def _periodic_purge(sessionmaker) -> None:
    """Background loop that trims event tables every hour and rolls up dailies.

    rate_events: keep 2 days (only rpm/rpd windows matter).
    client_rate_events: keep 2 days (only per-minute window matters).
    usage_events: keep 90 days (feeds the real-time analytics dashboard).
    usage_daily_rollup: keep 730 days (feeds historical analytics).

    Rollup order: compute today + yesterday rollups BEFORE purging usage_events,
    so a late-arrival row never falls off the 90d edge without being counted.
    """
    from datetime import datetime, timedelta, timezone
    while True:
        await asyncio.sleep(3600)
        try:
            async with sessionmaker() as session:
                usage_repo = UsageRepository(session)
                # Roll up yesterday (closes out late arrivals) then today
                # (still-accumulating, but kept fresh for historical views).
                today_utc = datetime.now(timezone.utc).date()
                yesterday_utc = today_utc - timedelta(days=1)
                rolled_yday = await usage_repo.rollup_day(yesterday_utc)
                rolled_today = await usage_repo.rollup_day(today_utc)

                r1 = await RateRepository(session).purge_old_events(86400 * 2)
                r2 = await ClientRateRepository(session).purge_older_than(86400 * 2)
                r3 = await usage_repo.purge_older_than(86400 * 90)
                r4 = await usage_repo.purge_rollups_older_than(730)
                await session.commit()
                if r1 or r2 or r3 or r4:
                    purge_rows_total.labels(table="rate_events").inc(r1)
                    purge_rows_total.labels(table="client_rate_events").inc(r2)
                    purge_rows_total.labels(table="usage_events").inc(r3)
                    purge_rows_total.labels(table="usage_daily_rollup").inc(r4)
                log.info(
                    "periodic_purge",
                    rate_events=r1, client_rate_events=r2,
                    usage_events=r3, usage_daily_rollup=r4,
                    rollup_rows_yesterday=rolled_yday,
                    rollup_rows_today=rolled_today,
                )
        except Exception as exc:  # noqa: BLE001
            log.error("periodic_purge_failed", error=str(exc))


# ──────────────────────────── app ────────────────────────────


app = FastAPI(
    title="FreeAI Orchestrator",
    description="Unified API that orchestrates multiple free AI provider tiers.",
    version="0.5.0",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Admin-Token",
        "X-Bootstrap-Token",
        "X-Request-ID",
    ],
)


# Max request body sizes. Audio uploads get a larger budget because they are
# binary; chat/JSON endpoints are bound tighter to blunt data-URI flooding.
_MAX_BODY_BYTES_AUDIO = 25 * 1024 * 1024   # 25 MB
_MAX_BODY_BYTES_DEFAULT = 10 * 1024 * 1024  # 10 MB


def _body_limit_for(path: str) -> int:
    if path.startswith("/v1/audio/"):
        return _MAX_BODY_BYTES_AUDIO
    return _MAX_BODY_BYTES_DEFAULT


@app.middleware("http")
async def enforce_body_size(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        limit = _body_limit_for(request.url.path)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    return Response(
                        content=f'{{"detail":"request body exceeds {limit} bytes"}}',
                        status_code=413,
                        media_type="application/json",
                    )
            except ValueError:
                pass
    return await call_next(request)


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    # Frontend uses inline style attrs + Google Fonts + fetch() to same origin.
    # No inline scripts (all JS lives in /frontend/*.js), so script-src stays tight.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.middleware("http")
async def no_cache_static_assets(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.endswith((".js", ".css", ".html")) or path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ──────────────────────────── first-run setup (public) ────────────────────────────


class SetupStatusResponse(BaseModel):
    needs_initial_setup: bool
    provider_names: list[str]


class InitialSetupBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    admin_token: str = Field(..., min_length=12, max_length=512)
    admin_token_confirm: str = Field(..., min_length=12, max_length=512)
    provider_keys: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _tokens_match(self) -> Self:
        if self.admin_token != self.admin_token_confirm:
            raise ValueError("admin tokens do not match")
        return self


async def _needs_initial_setup(session: AsyncSession) -> bool:
    settings = get_settings()
    if settings.admin_token:
        return False
    if settings.admin_token_path.exists():
        return False
    row = await session.get(AppConfigRow, 1)
    if row and row.admin_token_hash:
        return False
    cfg = ConfigRepository(session)
    return await cfg.count_providers_with_stored_keys() == 0


@app.get("/api/setup/status", response_model=SetupStatusResponse)
async def setup_status(session: AsyncSession = Depends(get_session)) -> SetupStatusResponse:
    return SetupStatusResponse(
        needs_initial_setup=await _needs_initial_setup(session),
        provider_names=sorted(DEFAULT_PROVIDERS.keys()),
    )


_SETUP_ADVISORY_LOCK_KEY = 98172354  # arbitrary but stable; pg_advisory_xact_lock


@app.post("/api/setup/initial", status_code=201)
async def setup_initial(
    body: InitialSetupBody,
    session: AsyncSession = Depends(get_session),
    x_bootstrap_token: Optional[str] = Header(default=None, alias="X-Bootstrap-Token"),
) -> dict:
    # Serialize the full setup flow — two concurrent callers would otherwise
    # both pass the _needs_initial_setup check and race on set_admin_token_hash.
    from sqlalchemy import text as _sql_text
    await session.execute(
        _sql_text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _SETUP_ADVISORY_LOCK_KEY},
    )
    if not await _needs_initial_setup(session):
        raise HTTPException(
            status_code=403,
            detail=(
                "initial setup is not available — already completed, or set "
                "FREEAI_ADMIN_TOKEN / data/admin_token"
            ),
        )
    if not verify_bootstrap_token(x_bootstrap_token):
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-Bootstrap-Token header. The one-time "
                "bootstrap token was printed to the server logs on startup "
                "(data/.bootstrap_token)."
            ),
        )
    cfg = ConfigRepository(session)
    await cfg.seed_defaults_if_empty()
    await cfg.set_admin_token_hash(hash_admin_token(body.admin_token))
    for name, key in body.provider_keys.items():
        kn = (name or "").strip().lower()
        if kn not in DEFAULT_PROVIDERS:
            continue
        v = (key or "").strip()
        if not v:
            continue
        await cfg.patch_provider(kn, api_key=v)
    consume_bootstrap_token()
    log.info("initial_setup_completed")
    return {
        "ok": True,
        "detail": (
            "Token de administrador guardado (hash en base de datos). "
            "Las claves de proveedor se almacenan cifradas como siempre. "
            "No volveremos a mostrar el token — cópialo ahora."
        ),
    }


# ──────────────────────────── auth endpoints ────────────────────────────


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


async def _issue_tokens(
    user_id: int, username: str, role: str, session: AsyncSession,
) -> dict:
    """Create access + refresh tokens and persist the refresh hash."""
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


@app.get("/api/auth/status")
async def auth_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Check if the system needs user migration or first-time registration."""
    user_repo = UserRepository(session)
    user_count = await user_repo.count()

    # Check if the only user is the migration placeholder
    if user_count == 1:
        placeholder = await user_repo.find_by_username("admin")
        if placeholder and placeholder.password_hash == "__placeholder_needs_migration__":
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


@app.post("/api/auth/register", status_code=201)
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
    from sqlalchemy import text as _sql_text
    await session.execute(
        _sql_text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _SETUP_ADVISORY_LOCK_KEY},
    )

    user_repo = UserRepository(session)
    count = await user_repo.count()

    # Don't count the migration placeholder as a real user
    placeholder = await user_repo.find_by_username("admin")
    is_placeholder = placeholder and placeholder.password_hash == "__placeholder_needs_migration__"
    real_count = count - (1 if is_placeholder else 0)

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
        from .auth import decode_access_token
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
    if existing and not (existing == placeholder and is_placeholder):
        raise HTTPException(409, f"username '{body.username}' is already taken")

    role = "admin" if real_count == 0 else "user"
    pwd_hash = hash_password(body.password)
    user_dto = await user_repo.create(body.username, pwd_hash, role=role)

    # If this is the first real admin and a placeholder exists,
    # transfer its providers, clients, and usage data to the new user
    if role == "admin" and is_placeholder and placeholder:
        from sqlalchemy import update as sa_update
        from .db.models import ClientRow, UserProviderRow, UsageEventRow, RateEventRow, ProviderStatsRow
        for tbl, col in [
            (UserProviderRow, UserProviderRow.user_id),
            (ClientRow, ClientRow.user_id),
        ]:
            await session.execute(
                sa_update(tbl).where(col == placeholder.id).values(user_id=user_dto.id)
            )
        # Also transfer rate_events and provider_stats
        await session.execute(
            sa_update(RateEventRow).where(RateEventRow.user_id == placeholder.id).values(user_id=user_dto.id)
        )
        await session.execute(
            sa_update(UsageEventRow).where(UsageEventRow.user_id == placeholder.id).values(user_id=user_dto.id)
        )
        # Transfer provider_stats — delete old PK rows and re-insert would be complex,
        # so just delete the placeholder's stats (they'll be re-created on first request)
        from sqlalchemy import delete as sa_delete
        await session.execute(
            sa_delete(ProviderStatsRow).where(ProviderStatsRow.user_id == placeholder.id)
        )
        # Delete the placeholder user
        await user_repo.delete(placeholder.id)
        await session.flush()

    if role == "admin":
        consume_bootstrap_token()

    return await _issue_tokens(user_dto.id, user_dto.username, user_dto.role, session)


_LOGIN_ATTEMPT_WINDOW_SECONDS = 300  # 5 min
_LOGIN_ATTEMPT_MAX = 10
# keyed by (ip, username_lower) → deque[timestamp]
_login_attempts: dict[tuple[str, str], "__import__('collections').deque[float]"] = {}  # type: ignore[assignment]


def _client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For when we know a proxy is in front. Default to
    # the peer address to prevent header spoofing from bypassing the limit.
    peer = request.client.host if request.client else "unknown"
    return peer


def _check_login_rate(ip: str, username: str) -> bool:
    """Return False when the caller has exceeded the window budget."""
    from collections import deque
    key = (ip, username.lower())
    now = time.time()
    dq = _login_attempts.setdefault(key, deque())
    while dq and dq[0] < now - _LOGIN_ATTEMPT_WINDOW_SECONDS:
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


def _clear_login_attempts(ip: str, username: str) -> None:
    _login_attempts.pop((ip, username.lower()), None)


@app.post("/api/auth/login")
async def login(
    body: LoginBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    ip = _client_ip(request)
    if not _check_login_rate(ip, body.username):
        raise HTTPException(
            429,
            f"too many login attempts — try again in {_LOGIN_ATTEMPT_WINDOW_SECONDS}s",
        )
    user_repo = UserRepository(session)
    user = await user_repo.find_by_username(body.username)
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "invalid username or password")
    _clear_login_attempts(ip, body.username)
    return await _issue_tokens(user.id, user.username, user.role, session)


@app.post("/api/auth/refresh")
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
    return await _issue_tokens(user.id, user.username, user.role, session)


@app.post("/api/auth/logout")
async def logout(body: LogoutBody, session: AsyncSession = Depends(get_session)) -> dict:
    refresh_repo = RefreshTokenRepository(session)
    token_hash = refresh_repo.hash_token(body.refresh_token)
    await refresh_repo.delete_by_hash(token_hash)
    return {"ok": True}


@app.get("/api/auth/me")
async def auth_me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"id": user.id, "username": user.username, "role": user.role}


@app.post("/api/auth/migrate-token", status_code=201)
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
    from .security import verify_admin_credentials

    ip = _client_ip(request)
    if not _check_login_rate(ip, "__migrate_token__"):
        raise HTTPException(
            429,
            f"too many attempts — try again in {_LOGIN_ATTEMPT_WINDOW_SECONDS}s",
        )

    # Serialize against concurrent calls so only one migration attempt wins.
    from sqlalchemy import text as _sql_text
    await session.execute(
        _sql_text("SELECT pg_advisory_xact_lock(:k)"),
        {"k": _SETUP_ADVISORY_LOCK_KEY},
    )

    user_repo = UserRepository(session)

    # Migration is only valid while the only real user (if any) is the
    # placeholder inserted by migration 0013. Once any real user exists —
    # even alongside the placeholder — this endpoint is permanently closed.
    placeholder = await user_repo.find_by_username("admin")
    is_placeholder = placeholder and placeholder.password_hash == "__placeholder_needs_migration__"
    total_users = await user_repo.count()
    real_user_count = total_users - (1 if is_placeholder else 0)
    if real_user_count > 0:
        raise HTTPException(400, "migration already completed — users exist")

    if not await verify_admin_credentials(session, body.admin_token):
        raise HTTPException(401, "invalid admin token")
    _clear_login_attempts(ip, "__migrate_token__")

    pwd_hash = hash_password(body.password)

    if is_placeholder:
        # Update the placeholder with real credentials
        from sqlalchemy import update
        from .db.models import UserRow
        await session.execute(
            update(UserRow).where(UserRow.id == placeholder.id).values(
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
    return await _issue_tokens(user_dto.id, user_dto.username, user_dto.role, session)


# ──────────────────────────── user management (admin) ────────────────────────────


@app.get("/api/users")
async def list_users(
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin_user),
) -> list[dict]:
    user_repo = UserRepository(session)
    users = await user_repo.list_all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "max_clients": u.max_clients,
            "created_at": u.created_at,
        }
        for u in users
    ]


@app.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin_user),
) -> dict:
    if user_id == admin.id:
        raise HTTPException(400, "cannot delete yourself")
    user_repo = UserRepository(session)
    if not await user_repo.delete(user_id):
        raise HTTPException(404, "user not found")
    return {"ok": True}


class ResetPasswordBody(BaseModel):
    password: str = Field(..., min_length=8, max_length=512)


@app.post("/api/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    body: ResetPasswordBody,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin_user),
) -> dict:
    user_repo = UserRepository(session)
    pwd_hash = hash_password(body.password)
    if not await user_repo.update_password(user_id, pwd_hash):
        raise HTTPException(404, "user not found")
    # Invalidate all refresh tokens for that user
    refresh_repo = RefreshTokenRepository(session)
    await refresh_repo.delete_all_for_user(user_id)
    return {"ok": True}


@app.get("/api/users/analytics")
async def users_analytics(
    days: int = 7,
    session: AsyncSession = Depends(get_session),
    admin: CurrentUser = Depends(require_admin_user),
) -> dict:
    """Per-user summary for the admin Users panel.

    Returns one row per user with provider-key counts, client counts, 7d usage
    totals, and a daily activity series (calls/day for the last N days) so the
    frontend can render a comparison chart without N+1 calls.
    """
    if days < 1 or days > 30:
        raise HTTPException(400, "days must be between 1 and 30")

    from datetime import datetime as _datetime, timezone as _timezone

    from sqlalchemy import text as sa_text

    # One batch query for each aggregate. All keyed by user_id.
    users_rows = (await session.execute(sa_text(
        "SELECT id, username, role, max_clients, created_at FROM users ORDER BY id"
    ))).all()

    provider_rows = (await session.execute(sa_text(
        """
        SELECT user_id,
               COUNT(*) AS configured,
               COUNT(*) FILTER (WHERE enabled AND api_key_encrypted IS NOT NULL) AS active
        FROM user_providers
        GROUP BY user_id
        """
    ))).all()
    providers_by_user = {r.user_id: (r.configured, r.active) for r in provider_rows}

    client_rows = (await session.execute(sa_text(
        """
        SELECT user_id,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE enabled) AS enabled
        FROM clients
        GROUP BY user_id
        """
    ))).all()
    clients_by_user = {r.user_id: (r.total, r.enabled) for r in client_rows}

    # 7d (or N-day) totals from raw events — accurate, and 7 days stays fast.
    window_seconds = days * 86400
    now = time.time()
    since = now - window_seconds

    usage_rows = (await session.execute(sa_text(
        """
        SELECT user_id,
               COUNT(*) AS calls,
               COUNT(*) FILTER (WHERE outcome = 'success') AS success,
               COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
               MAX(occurred_at) AS last_seen
        FROM usage_events
        WHERE occurred_at >= :since AND user_id IS NOT NULL
        GROUP BY user_id
        """
    ).bindparams(since=since))).all()
    usage_by_user = {
        r.user_id: {
            "calls": int(r.calls),
            "success": int(r.success),
            "tokens": int(r.tokens),
            "last_seen": float(r.last_seen) if r.last_seen else None,
        }
        for r in usage_rows
    }

    # Daily activity buckets — one row per (user_id, day) in the window.
    # floor(occurred_at / 86400) gives a stable day index (UTC).
    day_seconds = 86400
    first_day_index = int(since // day_seconds)
    daily_rows = (await session.execute(sa_text(
        f"""
        SELECT user_id,
               CAST(FLOOR(occurred_at / {day_seconds}) AS BIGINT) AS day_index,
               COUNT(*) AS calls
        FROM usage_events
        WHERE occurred_at >= :since AND user_id IS NOT NULL
        GROUP BY user_id, day_index
        ORDER BY user_id, day_index
        """
    ).bindparams(since=since))).all()
    daily_by_user: dict[int, dict[int, int]] = {}
    for r in daily_rows:
        daily_by_user.setdefault(r.user_id, {})[int(r.day_index)] = int(r.calls)

    # Dense day list so the frontend can plot without gap handling.
    day_indices = [first_day_index + i for i in range(days)]

    out = []
    for u in users_rows:
        pc, pa = providers_by_user.get(u.id, (0, 0))
        cc, ce = clients_by_user.get(u.id, (0, 0))
        use = usage_by_user.get(u.id, {"calls": 0, "success": 0, "tokens": 0, "last_seen": None})
        series = daily_by_user.get(u.id, {})
        daily = [
            {"day": (_datetime.fromtimestamp(idx * day_seconds, tz=_timezone.utc)
                     .date().isoformat()),
             "calls": series.get(idx, 0)}
            for idx in day_indices
        ]
        calls = use["calls"]
        success_rate = (use["success"] / calls) if calls > 0 else None
        out.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "max_clients": u.max_clients,
            "created_at": float(u.created_at),
            "providers_configured": int(pc),
            "providers_active": int(pa),
            "clients_configured": int(cc),
            "clients_enabled": int(ce),
            "calls": calls,
            "success": use["success"],
            "tokens": use["tokens"],
            "success_rate": success_rate,
            "last_seen": use["last_seen"],
            "daily": daily,
        })

    return {
        "days": days,
        "window_start": since,
        "window_end": now,
        "users": out,
    }


# ──────────────────────────── per-user provider credentials ────────────────────────────


class UserProviderUpdate(BaseModel):
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    rpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    tpd_limit: Optional[int] = None
    weight: Optional[float] = None
    default_model: Optional[str] = None
    max_retries: Optional[int] = None


@app.get("/api/me/providers")
async def list_my_providers(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    """List the current user's provider configs (keys masked)."""
    repo = UserProviderRepository(session)
    dtos = await repo.list_for_user(user.id)
    log.info(
        "list_my_providers",
        user_id=user.id, username=user.username, role=user.role,
        providers_found=len(dtos),
        with_key=sum(1 for d in dtos if d.api_key),
    )
    return [repo.mask_dto(d) for d in dtos]


@app.get("/api/me/providers/debug")
async def debug_my_providers(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Debug endpoint — shows where keys actually are and if decrypt works."""
    from sqlalchemy import select, func
    from .db.models import UserProviderRow, ProviderConfigRow
    from .crypto import decrypt
    # Count user_providers per user_id
    up_counts = (await session.execute(
        select(UserProviderRow.user_id, func.count()).group_by(UserProviderRow.user_id)
    )).all()
    # Count catalog keys
    catalog_keys = (await session.execute(
        select(func.count()).select_from(ProviderConfigRow)
        .where(ProviderConfigRow.api_key_encrypted.isnot(None))
    )).scalar_one()
    # This user's providers with decrypt check
    my_rows = (await session.execute(
        select(UserProviderRow)
        .where(UserProviderRow.user_id == user.id)
    )).scalars().all()
    providers_detail = []
    for r in my_rows:
        has_encrypted = r.api_key_encrypted is not None
        decrypted = decrypt(r.api_key_encrypted) if has_encrypted else None
        providers_detail.append({
            "name": r.provider_name,
            "has_encrypted_value": has_encrypted,
            "decrypt_ok": decrypted is not None,
            "has_key": bool(decrypted),
        })
    # Master key info
    from .crypto import MASTER_KEY_PATH
    master_key_source = "env" if os.environ.get("FREEAI_MASTER_KEY") else (
        "file" if MASTER_KEY_PATH.exists() else "auto-generated"
    )
    return {
        "your_user_id": user.id,
        "your_username": user.username,
        "your_role": user.role,
        "user_providers_by_user": {str(uid): cnt for uid, cnt in up_counts},
        "catalog_keys_remaining": catalog_keys,
        "your_providers": providers_detail,
        "master_key_source": master_key_source,
        "master_key_path": str(MASTER_KEY_PATH),
        "cors_origins": get_settings().cors_origin_list,
    }


@app.get("/api/me/providers/catalog")
async def provider_catalog(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    """List all available providers (catalog) without keys."""
    repo = UserProviderRepository(session)
    return await repo.list_catalog()


@app.patch("/api/me/providers/{name}")
async def update_my_provider(
    name: str,
    patch: UserProviderUpdate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Upsert the current user's credentials/config for a provider."""
    repo = UserProviderRepository(session)
    fields = patch.model_dump(exclude_unset=True)
    if "api_key" in fields and fields["api_key"] == "":
        fields["api_key"] = None
    try:
        dto = await repo.upsert(user.id, name, **fields)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return repo.mask_dto(dto)


@app.delete("/api/me/providers/{name}")
async def delete_my_provider(
    name: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    repo = UserProviderRepository(session)
    if not await repo.delete(user.id, name):
        raise HTTPException(404, f"no configuration for provider '{name}'")
    return {"ok": True}


# ──────────────────────────── middleware ────────────────────────────


@app.middleware("http")
async def request_observability(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    started = time.perf_counter()
    try:
        response: Response = await call_next(request)
    except Exception as e:
        elapsed = time.perf_counter() - started
        log.error("unhandled_exception", error=str(e), elapsed=elapsed)
        raise
    elapsed = time.perf_counter() - started
    response.headers["X-Request-ID"] = request_id
    if request.url.path != "/metrics":
        route = request.scope.get("route")
        path_label = route.path if route else request.url.path
        http_requests_total.labels(
            method=request.method,
            path=path_label,
            status=str(response.status_code),
        ).inc()
        http_request_duration_seconds.labels(
            method=request.method, path=path_label
        ).observe(elapsed)
    return response


# ──────────────────────────── error mapping ────────────────────────────

_KIND_TO_STATUS = {
    ErrorKind.AUTH:         502,
    ErrorKind.RATE_LIMITED: 503,
    ErrorKind.CLIENT_ERROR: 400,
    ErrorKind.SERVER_ERROR: 502,
    ErrorKind.NETWORK:      504,
    ErrorKind.PARSING:      502,
    ErrorKind.UNKNOWN:      502,
}


def _http_from_provider_error(e: ProviderError) -> HTTPException:
    return HTTPException(
        status_code=_KIND_TO_STATUS.get(e.kind, 502),
        detail={"provider": e.provider, "kind": e.kind.value, "message": e.message},
    )


def get_orchestrator(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


# ──────────────────────────── models (OpenAI-compatible) ────────────────────────────


@app.get("/v1/models")
async def list_models() -> dict:
    """OpenAI-compatible /v1/models — lists FreeAI virtual models.

    Each virtual model maps to an internal routing strategy.  Clients
    can use any of these as the ``model`` parameter in chat completions.
    """
    return {
        "object": "list",
        "data": [
            {
                "id": vm.id,
                "object": "model",
                "created": 0,
                "owned_by": "freeai",
                "description": vm.description,
            }
            for vm in VIRTUAL_MODELS
        ],
    }


# ──────────────────────────── chat completions ────────────────────────────


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    orch: Orchestrator = Depends(get_orchestrator),
    client=Depends(require_client),
):
    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    usage_repo = UsageRepository(session)
    strategy_repo = StrategyRepository(session)
    user_provider_repo = UserProviderRepository(session)
    client_hash = client.key_hash if client else None
    user_id = getattr(request.state, "user_id", None)

    if user_id is None:
        raise HTTPException(400, "no user context — authenticate with a client key bound to a user")

    if req.stream:
        async def event_stream():
            try:
                async for chunk in orch.stream(
                    req, user_id, user_provider_repo,
                    config_repo, rate_repo, usage_repo, strategy_repo,
                    client_hash=client_hash,
                ):
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except ProviderError as e:
                err = {"error": {"provider": e.provider, "kind": e.kind.value, "message": e.message}}
                yield f"data: {json.dumps(err)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        return await orch.chat(
            req, user_id, user_provider_repo,
            config_repo, rate_repo, usage_repo, strategy_repo,
            client_hash=client_hash,
        )
    except ProviderError as e:
        raise _http_from_provider_error(e)


# ──────────────────────────── audio transcriptions ────────────────────────────

from .transcription import (
    TRANSCRIPTION_PROVIDERS,
    AudioInput,
    TranscriptionError,
    TranscriptionResult,
    resolve_content_type,
    supports_transcription,
    transcribe,
)


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    client=Depends(require_client),
):
    """OpenAI-compatible audio transcription with multi-provider fallback.

    Tries providers in priority order (Groq Whisper → Gemini). Each
    provider is checked for: configured API key, enabled, and available
    capacity. On transient failure the next provider is attempted.

    The response always follows the OpenAI format: ``{"text": "..."}``
    with additional ``provider`` and ``model`` fields.
    """
    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    usage_repo = UsageRepository(session)
    user_provider_repo = UserProviderRepository(session)
    client_hash = client.key_hash if client else None
    user_id = getattr(request.state, "user_id", None)

    if user_id is None:
        raise HTTPException(400, "no user context — authenticate with a client key bound to a user")

    # ── Prepare audio input (read once, reuse across attempts) ──
    file_bytes = await file.read()
    if len(file_bytes) > _MAX_BODY_BYTES_AUDIO:
        raise HTTPException(
            413,
            f"audio payload exceeds {_MAX_BODY_BYTES_AUDIO} bytes",
        )
    audio = AudioInput(
        file_bytes=file_bytes,
        filename=file.filename or "audio.ogg",
        content_type=resolve_content_type(file.filename, file.content_type),
        language=language,
    )

    # ── Collect eligible providers from user's configured providers ──
    user_providers = await user_provider_repo.list_for_user(user_id)
    candidates: list[tuple[str, ProviderConfigDTO]] = []
    for name in TRANSCRIPTION_PROVIDERS:
        if not supports_transcription(name):
            continue
        # Find in user's providers
        up = next((p for p in user_providers if p.provider_name == name), None)
        if up and up.api_key and up.enabled:
            dto = Orchestrator._user_provider_to_config(up)
            candidates.append((name, dto))

    if not candidates:
        raise HTTPException(
            400,
            "No transcription provider configured — add an API key for Groq or Gemini",
        )

    # ── Fallback loop: try each provider in priority order ──
    errors: list[dict] = []       # track every attempt for diagnostics
    fallback_position = 0

    for provider_name, dto in candidates:
        fallback_position += 1

        # Reserve capacity
        reservation = await rate_repo.try_reserve(
            user_id, provider_name, dto.rpm_limit, dto.rpd_limit,
        )
        if reservation is None:
            errors.append({"provider": provider_name, "skipped": "at capacity"})
            continue

        reservation_settled = False
        try:
            # Attempt transcription
            result = await transcribe(
                provider_name, audio, dto.api_key,
                client=app.state.orchestrator._client,
            )

            if isinstance(result, TranscriptionResult):
                # ── Success ──
                await rate_repo.commit(reservation, result.latency_ms, ok=True)
                reservation_settled = True
                await usage_repo.record(UsageEvent(
                    provider=result.provider, model=result.model,
                    strategy="transcription", outcome="success",
                    latency_ms=result.latency_ms, client_hash=client_hash,
                    user_id=user_id, fallback_position=fallback_position,
                ))
                return {
                    "text": result.text,
                    "provider": result.provider,
                    "model": result.model,
                    "latency_ms": result.latency_ms,
                    "fallback_position": fallback_position,
                }

            # ── Failure: commit error and decide whether to continue ──
            err = result
            errors.append({
                "provider": err.provider,
                "kind": err.kind.value,
                "message": err.message[:200],
            })

            quarantine_s = None
            if err.kind == ErrorKind.SERVER_ERROR:
                quarantine_s = 60
            elif err.kind == ErrorKind.NETWORK:
                quarantine_s = 30

            await rate_repo.commit(
                reservation, err.latency_ms, ok=False,
                error=err.message, error_kind=err.kind.value,
                quarantine_seconds=quarantine_s,
            )
            reservation_settled = True
            await usage_repo.record(UsageEvent(
                provider=err.provider, model=err.model,
                strategy="transcription", outcome=err.kind.value,
                latency_ms=err.latency_ms, client_hash=client_hash,
                user_id=user_id, fallback_position=fallback_position,
            ))

            # Auth/client errors won't be fixed by trying another provider
            if err.kind in (ErrorKind.AUTH, ErrorKind.CLIENT_ERROR):
                break

            # Transient / rate-limit → try next provider
        finally:
            if not reservation_settled:
                try:
                    await rate_repo.rollback(reservation)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "transcription reservation rollback failed",
                        provider=provider_name, exc_info=True,
                    )

    # ── All providers exhausted ──
    last = errors[-1] if errors else {}
    status = _KIND_TO_STATUS.get(ErrorKind(last.get("kind", "unknown")), 502) if "kind" in last else 503
    raise HTTPException(status, {
        "message": "All transcription providers failed",
        "attempts": errors,
    })


# ──────────────────────────── embeddings ────────────────────────────

from .embeddings import EMBEDDING_PROVIDERS, build_embedding_provider, supports_embeddings
from .providers.base import EmbeddingResult


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    input: Union[str, list[str]] = Field(
        ..., description="String or list of strings to embed.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Embedding model name. Passed verbatim to the chosen "
                    "provider; if omitted, each provider uses its configured "
                    "default (mistral-embed, text-embedding-004, …).",
    )
    preferred_provider: Optional[str] = Field(
        default=None,
        description="Force a specific provider (e.g. 'mistral'). If unset, "
                    "providers are tried in default priority order.",
    )
    fallback: bool = Field(
        default=True,
        description="If True, on transient failures try the next configured "
                    "provider. Set to False to fail fast on the first attempt.",
    )


@app.post("/v1/embeddings")
async def embeddings_endpoint(
    req: EmbeddingRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    client=Depends(require_client),
):
    """OpenAI-compatible embeddings with multi-provider fallback.

    Supported providers (in default priority order):
      1. **mistral** — OpenAI wire format, `mistral-embed` (1024 dim).
      2. **gemini** — Google v1beta, `text-embedding-004` (768 dim).

    Response follows OpenAI's shape:

        {
          "object": "list",
          "data": [{"object": "embedding", "index": 0, "embedding": [...]}, ...],
          "model": "mistral-embed",
          "provider": "mistral",
          "usage": {"prompt_tokens": N, "total_tokens": N},
          "fallback_position": 1
        }

    Embeddings are only comparable to other embeddings from the **same model**.
    If you switch providers you must re-embed your corpus.
    """
    rate_repo = RateRepository(session)
    usage_repo = UsageRepository(session)
    user_provider_repo = UserProviderRepository(session)
    client_hash = client.key_hash if client else None
    user_id = getattr(request.state, "user_id", None)

    if user_id is None:
        raise HTTPException(400, "no user context — authenticate with a client key bound to a user")

    # Normalize input to a list of strings
    texts: list[str] = [req.input] if isinstance(req.input, str) else list(req.input)
    if not texts:
        raise HTTPException(400, "input must be a non-empty string or list of strings")
    if any(not isinstance(t, str) for t in texts):
        raise HTTPException(400, "all input entries must be strings")

    # Build candidate list from the user's configured providers, intersected
    # with the set of providers that implement embeddings. If the caller
    # specified preferred_provider, that one goes first.
    user_providers = await user_provider_repo.list_for_user(user_id)
    priority = list(EMBEDDING_PROVIDERS)
    if req.preferred_provider:
        if req.preferred_provider not in priority:
            raise HTTPException(400, f"provider '{req.preferred_provider}' does not support embeddings")
        priority = [req.preferred_provider] + [p for p in priority if p != req.preferred_provider]

    candidates: list[tuple[str, ProviderConfigDTO]] = []
    for name in priority:
        if not supports_embeddings(name):
            continue
        up = next((p for p in user_providers if p.provider_name == name), None)
        if up and up.api_key and up.enabled:
            dto = Orchestrator._user_provider_to_config(up)
            candidates.append((name, dto))

    if not candidates:
        raise HTTPException(
            400,
            "No embedding provider configured — add an API key for Mistral or Gemini",
        )

    if not req.fallback:
        candidates = candidates[:1]

    errors: list[dict] = []
    fallback_position = 0

    for provider_name, dto in candidates:
        fallback_position += 1

        # Reserve capacity
        reservation = await rate_repo.try_reserve(
            user_id, provider_name, dto.rpm_limit, dto.rpd_limit,
        )
        if reservation is None:
            errors.append({"provider": provider_name, "skipped": "at capacity"})
            continue

        provider = build_embedding_provider(
            provider_name, api_key=dto.api_key, default_model=dto.default_model,
        )
        started = time.time()
        reservation_settled = False
        try:
            try:
                result: EmbeddingResult = await provider.embed(
                    texts, model=req.model, client=app.state.orchestrator._client,
                )
            except ProviderError as err:
                latency_ms = int((time.time() - started) * 1000)
                errors.append({
                    "provider": err.provider,
                    "kind": err.kind.value,
                    "message": err.message[:200],
                })

                quarantine_s = None
                if err.kind == ErrorKind.SERVER_ERROR:
                    quarantine_s = 60
                elif err.kind == ErrorKind.NETWORK:
                    quarantine_s = 30

                await rate_repo.commit(
                    reservation, latency_ms, ok=False,
                    error=err.message, error_kind=err.kind.value,
                    quarantine_seconds=quarantine_s,
                )
                reservation_settled = True
                await usage_repo.record(UsageEvent(
                    provider=err.provider, model=req.model or "",
                    strategy="embedding", outcome=err.kind.value,
                    latency_ms=latency_ms, client_hash=client_hash,
                    user_id=user_id, fallback_position=fallback_position,
                ))

                # Auth/client errors won't be fixed by trying another provider
                if err.kind in (ErrorKind.AUTH, ErrorKind.CLIENT_ERROR):
                    break
                continue
        finally:
            if not reservation_settled:
                # Request cancelled or unexpected exception before outcome
                # recorded — release the reservation so rate counters don't
                # carry a phantom in-flight call against the user.
                try:
                    await rate_repo.rollback(reservation)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "embeddings reservation rollback failed",
                        provider=provider_name, exc_info=True,
                    )

        # ── Success ──
        latency_ms = int((time.time() - started) * 1000)
        await rate_repo.commit(
            reservation, latency_ms, ok=True,
            prompt_tokens=result.prompt_tokens, completion_tokens=0,
        )
        await usage_repo.record(UsageEvent(
            provider=result.provider, model=result.model,
            strategy="embedding", outcome="success",
            latency_ms=latency_ms, prompt_tokens=result.prompt_tokens,
            completion_tokens=0, client_hash=client_hash,
            user_id=user_id, fallback_position=fallback_position,
        ))
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(result.vectors)
            ],
            "model": result.model,
            "provider": result.provider,
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "total_tokens": result.prompt_tokens,
            },
            "fallback_position": fallback_position,
        }

    # ── All providers exhausted ──
    last = errors[-1] if errors else {}
    status = _KIND_TO_STATUS.get(ErrorKind(last.get("kind", "unknown")), 502) if "kind" in last else 503
    raise HTTPException(status, {
        "message": "All embedding providers failed",
        "attempts": errors,
    })


# ──────────────────────────── provider admin ────────────────────────────


class ProviderUpdate(BaseModel):
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    weight: Optional[float] = None
    default_model: Optional[str] = None
    rpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    tpd_limit: Optional[int] = None
    tags: Optional[list[str]] = None


class ProviderPatchResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    provider: ProviderStatus
    model_warning: Optional[str] = None
    model_suggestions: list[str] = Field(default_factory=list)


async def _provider_status(
    name: str, config_repo: ConfigRepository, rate_repo: RateRepository,
    user_id: int,
) -> ProviderStatus:
    dto = await config_repo.get_provider(name)
    if not dto:
        raise HTTPException(404, f"unknown provider '{name}'")
    snap = await rate_repo.snapshot(user_id, name)

    return ProviderStatus(
        name=name,
        enabled=dto.enabled,
        has_key=bool(dto.api_key),
        healthy=snap.healthy,
        requests_today=snap.requests_today,
        requests_this_minute=snap.requests_this_minute,
        rpm_limit=dto.rpm_limit,
        rpd_limit=dto.rpd_limit,
        tpd_limit=dto.tpd_limit,
        tokens_today=snap.tokens_today,
        weight=dto.weight,
        last_error=snap.last_error,
        last_latency_ms=snap.last_latency_ms,
        latency_ema_ms=snap.latency_ema_ms,
        tags=dto.tags,
        default_model=dto.default_model,
    )


@app.get("/api/providers", response_model=list[ProviderStatus])
async def list_providers(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_admin_user),
) -> list[ProviderStatus]:
    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    providers = await config_repo.list_providers()
    return [await _provider_status(p.name, config_repo, rate_repo, user.id) for p in providers]


@app.patch("/api/providers/{name}", response_model=ProviderPatchResponse)
async def update_provider(
    name: str,
    patch: ProviderUpdate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_admin_user),
) -> ProviderPatchResponse:
    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    fields = patch.model_dump(exclude_unset=True)
    if "api_key" in fields and fields["api_key"] == "":
        fields["api_key"] = None
    try:
        await config_repo.patch_provider(name, **fields)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if fields:
        await rate_repo.reset_health(user.id, name)

    # Model validation — soft: we accept unknown models but tell the user.
    model_warning: Optional[str] = None
    suggestions: list[str] = []
    if "default_model" in fields and fields["default_model"]:
        new_model = fields["default_model"]
        if not is_known(name, new_model):
            model_warning = (
                f"'{new_model}' is not in the known-models list for {name}. "
                "It may still work — FreeAI will pass it through to the provider."
            )
            suggestions = suggest_similar(name, new_model)

    status = await _provider_status(name, config_repo, rate_repo, user.id)
    return ProviderPatchResponse(
        provider=status,
        model_warning=model_warning,
        model_suggestions=suggestions,
    )


@app.post("/api/providers/{name}/reset")
async def reset_provider_health(
    name: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_admin_user),
) -> dict:
    rate_repo = RateRepository(session)
    await rate_repo.reset_health(user.id, name)
    return {"ok": True}


@app.get("/api/providers/{name}/models")
async def list_provider_models(
    name: str,
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    if name not in KNOWN_MODELS:
        raise HTTPException(404, f"unknown provider '{name}'")
    return {
        "provider": name,
        "models": [
            {
                "id": m.id,
                "context_window": m.context_window,
                "capabilities": m.capabilities,
                "note": m.note,
            }
            for m in KNOWN_MODELS[name]
        ],
    }


# ──────────────────────────── config ────────────────────────────


class StrategyUpdate(BaseModel):
    default_strategy: str  # not a Literal anymore — custom strategies allowed


class FallbackUpdate(BaseModel):
    enable_fallback: bool


@app.get("/api/config")
async def get_config(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    config_repo = ConfigRepository(session)
    strategy_repo = StrategyRepository(session)
    cfg = await config_repo.get_app_config()
    strategies = await strategy_repo.list_all()
    return {
        "default_strategy": cfg.default_strategy,
        "enable_fallback": cfg.enable_fallback,
        "available_strategies": [s.name for s in strategies],
        "available_providers": list(PROVIDER_REGISTRY.keys()),
    }


@app.put("/api/config/strategy")
async def set_strategy(
    payload: StrategyUpdate,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> dict:
    # Validate that the target strategy exists
    strategy_repo = StrategyRepository(session)
    if not await strategy_repo.get(payload.default_strategy):
        raise HTTPException(400, f"unknown strategy '{payload.default_strategy}'")
    config_repo = ConfigRepository(session)
    await config_repo.set_strategy(payload.default_strategy)
    return {"default_strategy": payload.default_strategy}


@app.put("/api/config/fallback")
async def set_fallback(
    payload: FallbackUpdate,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> dict:
    repo = ConfigRepository(session)
    await repo.set_fallback(payload.enable_fallback)
    return {"enable_fallback": payload.enable_fallback}


# ──────────────────────────── strategies ────────────────────────────


class StrategyUpsertIn(BaseModel):
    """Input shape for strategy create/update — DSL definition only.

    See app.strategy_dsl for the schema and docs/STRATEGY_DSL.md for
    the design rationale. The legacy `tags` field that bridged the
    transition was removed in commit 4 of the strategy DSL rework.
    """
    name: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    definition: Optional[dict] = None
    description: str = ""


class StrategyOut(BaseModel):
    name: str
    definition: Optional[dict] = None
    description: str
    is_builtin: bool


def _strategy_to_out(dto: StrategyDTO) -> StrategyOut:
    return StrategyOut(
        name=dto.name,
        definition=dto.definition,
        description=dto.description,
        is_builtin=dto.is_builtin,
    )


@app.get("/api/strategies", response_model=list[StrategyOut])
async def list_strategies(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> list[StrategyOut]:
    repo = StrategyRepository(session)
    return [_strategy_to_out(s) for s in await repo.list_all()]


def _validate_definition_or_422(definition: Optional[dict]) -> None:
    """Run the DSL parser; on failure raise 422 with the parser message."""
    try:
        parse_definition(definition)
    except ParseError as e:
        raise HTTPException(422, str(e))


@app.post("/api/strategies", response_model=StrategyOut)
async def create_strategy(
    payload: StrategyUpsertIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> StrategyOut:
    repo = StrategyRepository(session)
    existing = await repo.get(payload.name)
    if existing:
        raise HTTPException(409, f"strategy '{payload.name}' already exists — use PATCH to edit")
    _validate_definition_or_422(payload.definition)
    dto = StrategyDTO(
        name=payload.name,
        definition=payload.definition,
        description=payload.description,
        is_builtin=False,
    )
    saved = await repo.upsert(dto)
    request.app.state.orchestrator.invalidate_strategy_cache(payload.name)
    return _strategy_to_out(saved)


@app.patch("/api/strategies/{name}", response_model=StrategyOut)
async def update_strategy(
    name: str,
    payload: StrategyUpsertIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> StrategyOut:
    if payload.name != name:
        raise HTTPException(400, "strategy name in body must match the URL")
    repo = StrategyRepository(session)
    existing = await repo.get(name)
    if not existing:
        raise HTTPException(404, f"unknown strategy '{name}'")
    _validate_definition_or_422(payload.definition)
    saved = await repo.upsert(
        StrategyDTO(
            name=name,
            definition=payload.definition,
            description=payload.description,
            is_builtin=existing.is_builtin,
        )
    )
    request.app.state.orchestrator.invalidate_strategy_cache(name)
    return _strategy_to_out(saved)


@app.delete("/api/strategies/{name}")
async def delete_strategy(
    name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> dict:
    repo = StrategyRepository(session)
    try:
        deleted = await repo.delete(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, f"unknown strategy '{name}'")
    request.app.state.orchestrator.invalidate_strategy_cache(name)
    return {"ok": True}


# ──────────────────────────── tags vocabulary ────────────────────────────


class TagInfo(BaseModel):
    tag: str
    providers: list[str]


@app.get("/api/tags", response_model=list[TagInfo])
async def list_tags(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> list[TagInfo]:
    """Vocabulary discovery for the strategy editor.

    Returns every distinct tag currently in use by at least one provider,
    along with the list of providers carrying it. The frontend uses this
    to populate dropdowns in the form builder so users can only pick
    tags that will actually match something.
    """
    config_repo = ConfigRepository(session)
    providers = await config_repo.list_providers()
    bag: dict[str, list[str]] = {}
    for p in providers:
        for t in p.tags or []:
            bag.setdefault(t, []).append(p.name)
    return [TagInfo(tag=t, providers=sorted(names)) for t, names in sorted(bag.items())]


# ──────────────────────────── strategy preview ────────────────────────────


class StrategyPreviewIn(BaseModel):
    """Body for /api/strategies/preview — a candidate definition only.

    No `name` or `description` because the preview never touches the DB;
    it just runs the same ranker the orchestrator would use, with the
    candidate definition, against the live provider snapshots.
    """
    definition: Optional[dict] = None


class PreviewedCandidate(BaseModel):
    name: str
    score: float
    healthy: bool
    rpd_remaining: float
    last_latency_ms: Optional[int] = None


class StrategyPreviewOut(BaseModel):
    candidates: list[PreviewedCandidate]
    excluded: list[str]  # providers filtered out by require clauses
    warnings: list[str]  # soft notes from the parser/static analysis


@app.post("/api/strategies/preview", response_model=StrategyPreviewOut)
async def preview_strategy(
    payload: StrategyPreviewIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_admin_user),
) -> StrategyPreviewOut:
    """Run the ranker against `definition` without saving the strategy.

    Lets the editor show a live preview as the user builds clauses.
    Validation errors raise 422 — same as the create/update endpoints —
    so the editor can show field-level feedback. The preview itself
    only fails if the parser fails; an empty candidate list (everything
    excluded) is a valid preview, not an error.
    """
    try:
        defn = parse_definition(payload.definition)
    except ParseError as e:
        raise HTTPException(422, str(e))

    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    providers = await config_repo.list_providers()
    eligible = [p for p in providers if p.enabled and p.api_key]
    if not eligible:
        return StrategyPreviewOut(
            candidates=[],
            excluded=[p.name for p in providers if not (p.enabled and p.api_key)],
            warnings=["no providers configured with an API key"],
        )

    snapshots = await rate_repo.snapshot_all(user.id, [p.name for p in eligible])

    from .strategy_dsl import baseline_score as dsl_baseline
    from .strategy_dsl import context_from_provider
    from .strategy_dsl import score as dsl_score

    candidates: list[PreviewedCandidate] = []
    excluded: list[str] = [p.name for p in providers if not (p.enabled and p.api_key)]

    for dto in eligible:
        snap = snapshots.get(dto.name)
        if not snap:
            excluded.append(dto.name)
            continue
        if not snap.healthy:
            excluded.append(dto.name)
            continue

        ctx = context_from_provider(
            name=dto.name,
            enabled=dto.enabled,
            weight=dto.weight,
            tags=dto.tags,
            last_latency_ms=snap.last_latency_ms,
            latency_ema_ms=snap.latency_ema_ms,
            requests_today=snap.requests_today,
            requests_this_minute=snap.requests_this_minute,
            rpd_limit=dto.rpd_limit,
            rpm_limit=dto.rpm_limit,
            tokens_today=snap.tokens_today,
            total_failures=snap.total_failures,
        )
        contribution = dsl_score(defn, ctx)
        if contribution is None:
            excluded.append(dto.name)
            continue
        baseline = dsl_baseline(ctx)
        rpd_remaining = ctx.fields["rpd_remaining"]
        candidates.append(PreviewedCandidate(
            name=dto.name,
            score=baseline + contribution,
            healthy=snap.healthy,
            rpd_remaining=rpd_remaining,
            last_latency_ms=snap.last_latency_ms,
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)

    # Soft warnings: prefer clauses on tag values that no provider has.
    warnings: list[str] = []
    known_tags: set[str] = set()
    for p in providers:
        for t in p.tags or []:
            known_tags.add(t)
    for clause in (defn.prefer + defn.require):
        if clause.field == "tags" and clause.op == "contains" and clause.value not in known_tags:
            warnings.append(
                f"tag '{clause.value}' is not used by any current provider — "
                f"this clause won't fire until a provider is given that tag"
            )

    if not candidates:
        warnings.append(
            "no providers match this definition right now; the strategy "
            "would route nothing if saved as-is"
        )

    return StrategyPreviewOut(
        candidates=candidates,
        excluded=sorted(set(excluded)),
        warnings=warnings,
    )


# ──────────────────────────── analytics ────────────────────────────


@app.get("/api/analytics")
async def analytics(
    window_seconds: int = 24 * 3600,
    bucket_count: int = 24,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Aggregated usage summary. `window_seconds` and `bucket_count` let the
    frontend switch between "last hour / 12 buckets" and "last 24h / 24 buckets"
    etc."""
    if window_seconds < 60 or window_seconds > 7 * 24 * 3600:
        raise HTTPException(400, "window_seconds must be between 60 and 604800")
    if bucket_count < 1 or bucket_count > 168:
        raise HTTPException(400, "bucket_count must be between 1 and 168")
    repo = UsageRepository(session)
    # Every user sees only their own analytics
    summary = await repo.summary(
        window_seconds=window_seconds, bucket_count=bucket_count,
        user_id=_user.id,
    )
    return asdict(summary)


@app.get("/api/analytics/historical")
async def analytics_historical(
    days: int = 90,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Long-window aggregates read from usage_daily_rollup.

    Supports 30/90/180/365/730 days — computed from pre-aggregated daily rows
    so it stays fast even over a full year. Use /api/analytics for windows
    ≤ 7 days (finer granularity from raw events).
    """
    if days not in (30, 90, 180, 365, 730):
        raise HTTPException(400, "days must be one of 30, 90, 180, 365, 730")
    repo = UsageRepository(session)
    summary = await repo.historical_summary(days=days, user_id=_user.id)
    return asdict(summary)


# ──────────────────────────── clients ────────────────────────────


class ClientCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    rpm_limit: int = Field(default=60, ge=1, le=10_000)


class ClientCreated(BaseModel):
    name: str
    api_key: str
    key_hash: str
    rpm_limit: int


@app.get("/api/clients")
async def list_clients(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    repo = ClientRepository(session)
    # Every user sees only their own clients
    return [
        {
            "name": c.name, "key_hash": c.key_hash,
            "rpm_limit": c.rpm_limit, "enabled": c.enabled,
        }
        for c in await repo.list_all(user_id=user.id)
    ]


@app.post("/api/clients", response_model=ClientCreated, status_code=201)
async def create_client(
    payload: ClientCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ClientCreated:
    repo = ClientRepository(session)
    client, raw = await repo.create(payload.name, user.id, payload.rpm_limit)
    return ClientCreated(
        name=client.name,
        api_key=raw,
        key_hash=client.key_hash,
        rpm_limit=client.rpm_limit,
    )


@app.delete("/api/clients/{key_hash}")
async def revoke_client(
    key_hash: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    repo = ClientRepository(session)
    # Every user can only revoke their own clients
    if not await repo.revoke(key_hash, user_id=user.id):
        raise HTTPException(status_code=404, detail="client not found")
    return {"ok": True}


# ──────────────────────────── health + metrics ────────────────────────────


@app.get("/api/health")
async def health() -> dict:
    """Public healthcheck — minimal on purpose. Aggregate fleet counts are
    available to authenticated admins via /api/analytics and /api/providers.
    """
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)


# ──────────────────────────── static frontend ────────────────────────────

_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")

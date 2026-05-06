"""FreeAI FastAPI entrypoint.

Lifespan + middlewares only. Endpoint groups live in ``app.routers``;
provider selection / fallback live in ``app.orchestrator``; provider
adapters live in ``app.providers``. The CORS, security-header, body-size
and request-observability middlewares are mounted here because they
apply across every router.
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .bootstrap import ensure_bootstrap_token
from .crypto import (
    MasterKeyNotReadyError,
    ensure_pending_master_key,
    is_master_key_ready,
    master_key_confirmation_required,
    read_pending_master_key_plaintext,
)
from .db import create_engine_and_sessionmaker, dispose_engine
from .db.models import AppConfigRow
from .logging_config import configure_logging, get_logger
from .metrics import (
    http_request_duration_seconds,
    http_requests_total,
    purge_rows_total,
)
from .orchestrator import Orchestrator
from .providers import PROVIDER_REGISTRY
from .repositories import (
    ClientRateRepository,
    ClientRepository,
    ConfigRepository,
    RateRepository,
    StrategyRepository,
    UsageRepository,
    UserRepository,
)
from .routers import (
    analytics,
    auth,
    chat,
    clients,
    config,
    embeddings,
    health,
    me_providers,
    providers_admin,
    setup,
    strategies,
    transcriptions,
    users,
)
from .routers._common import body_limit_for, is_placeholder
from .settings import get_settings

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
        real_users = user_count - (1 if is_placeholder(placeholder) else 0)
        cfg_row = await session.get(AppConfigRow, 1)
        has_admin_token = bool(cfg_row and cfg_row.admin_token_hash) or bool(settings.admin_token) or settings.admin_token_path.exists()
        needs_bootstrap = real_users == 0 and not has_admin_token
        mk_plain = ensure_pending_master_key()
        if mk_plain:
            print(
                "\n"
                "============================================================\n"
                "  FreeAI encryption master key (paste in the web UI once):\n"
                f"    {mk_plain}\n"
                "  Open the web UI once: paste this key, the bootstrap token,\n"
                "  and your admin username/password (single FIRST SETUP form).\n"
                "  Pending at data/.master_key.pending until confirmed.\n"
                "============================================================\n",
                flush=True,
            )
        elif master_key_confirmation_required():
            pk = read_pending_master_key_plaintext()
            if pk:
                print(
                    "\n"
                    "============================================================\n"
                    "  FreeAI master key still awaiting UI confirmation — paste:\n"
                    f"    {pk}\n"
                    "  (Server was restarted before you confirmed the key.)\n"
                    "============================================================\n",
                    flush=True,
                )

        new_token = ensure_bootstrap_token(needed=needs_bootstrap)
        if new_token:
            print(
                "\n"
                "============================================================\n"
                "  FreeAI bootstrap token (one-time, do not share):\n"
                f"    {new_token}\n"
                "  Send it in the X-Bootstrap-Token header when calling\n"
                "  POST /api/setup/confirm-master-key, POST /api/setup/initial,\n"
                "  or POST /api/auth/register (first admin only).\n"
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

                purged = {
                    "rate_events": await RateRepository(session).purge_old_events(86400 * 2),
                    "client_rate_events": await ClientRateRepository(session).purge_older_than(86400 * 2),
                    "usage_events": await usage_repo.purge_older_than(86400 * 90),
                    "usage_daily_rollup": await usage_repo.purge_rollups_older_than(730),
                }
                await session.commit()
                if any(purged.values()):
                    for table, rows in purged.items():
                        purge_rows_total.labels(table=table).inc(rows)
                log.info(
                    "periodic_purge",
                    **purged,
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


@app.exception_handler(MasterKeyNotReadyError)
async def _master_key_not_ready_handler(_request: Request, exc: MasterKeyNotReadyError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


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


@app.middleware("http")
async def enforce_body_size(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        limit = body_limit_for(request.url.path)
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


# ──────────────────────────── routers ────────────────────────────

# Order matters for two reasons: (1) FastAPI matches routes in registration
# order, so put more specific routers before catch-all ones; (2) the static
# StaticFiles mount on "/" must come last because it eats every unmatched
# path. Within /api the order is irrelevant — prefixes don't collide.

app.include_router(setup.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(me_providers.router)
app.include_router(chat.router)
app.include_router(transcriptions.router)
app.include_router(embeddings.router)
app.include_router(providers_admin.router)
app.include_router(config.router)
app.include_router(strategies.router)
app.include_router(strategies.tags_router)
app.include_router(analytics.router)
app.include_router(clients.router)
app.include_router(health.router)


# ──────────────────────────── static frontend ────────────────────────────

_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")

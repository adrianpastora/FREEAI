"""FreeAI FastAPI entrypoint.

Wiring only: middlewares + router mounting + static file serving. The
endpoint groups live in ``app.routers``; provider selection lives in
``app.orchestrator``; provider adapters live in ``app.providers``;
startup / shutdown / migrations / periodic purge live in ``app.lifecycle``.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .crypto import MasterKeyNotReadyError
from .lifecycle import lifespan
from .logging_config import configure_logging, get_logger
from .metrics import http_request_duration_seconds, http_requests_total
from .routers import (
    analytics,
    auth,
    chat,
    clients,
    config,
    embeddings,
    health,
    me_providers,
    pricing_admin,
    providers_admin,
    setup,
    strategies,
    transcriptions,
    users,
)
from .routers._common import body_limit_for
from .settings import get_settings

configure_logging()
log = get_logger("freeai")


app = FastAPI(
    title="FreeAI Orchestrator",
    description="Unified API that orchestrates multiple free AI provider tiers.",
    version="0.6.0",
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
app.include_router(pricing_admin.router)
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

"""FreeAI FastAPI app — Sprint 5.

Sprint 5 improvements over Sprint 4:
  • Prometheus metrics use route templates (bounded cardinality)
  • Streaming no longer commits the DB session per chunk
  • _rank() uses batched queries (3 instead of 2×N per request)
  • Periodic purge task for rate_events, client_rate_events, usage_events
  • Strategy TTL cache (5s, invalidated on CRUD)
  • Streaming captures token counts via stream_options.include_usage
  • TTFB tracking on usage_events
  • Dead code removed (config_store, rate_tracker)
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
from typing import Optional, Self

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response
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
    RateRepository,
    StrategyDTO,
    StrategyRepository,
    UsageRepository,
)
from .repositories.config_repo import DEFAULT_PROVIDERS
from .schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderStatus,
)
from .security import require_admin, require_client
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
        for line in stdout.decode().splitlines():
            log.info("alembic_stdout", line=line)
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        log.error("migration_failed", returncode=proc.returncode, stderr=err)
        raise RuntimeError(f"alembic upgrade failed (exit {proc.returncode}): {err}")
    log.info("migrations_done")


async def _periodic_purge(sessionmaker) -> None:
    """Background loop that trims event tables every hour.

    rate_events: keep 2 days (only rpm/rpd windows matter).
    client_rate_events: keep 2 days (only per-minute window matters).
    usage_events: keep 90 days (feeds the analytics dashboard).
    """
    while True:
        await asyncio.sleep(3600)
        try:
            async with sessionmaker() as session:
                r1 = await RateRepository(session).purge_old_events(86400 * 2)
                r2 = await ClientRateRepository(session).purge_older_than(86400 * 2)
                r3 = await UsageRepository(session).purge_older_than(86400 * 90)
                await session.commit()
                if r1 or r2 or r3:
                    purge_rows_total.labels(table="rate_events").inc(r1)
                    purge_rows_total.labels(table="client_rate_events").inc(r2)
                    purge_rows_total.labels(table="usage_events").inc(r3)
                    log.info(
                        "periodic_purge",
                        rate_events=r1, client_rate_events=r2, usage_events=r3,
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
    allow_headers=["*"],
)


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


@app.post("/api/setup/initial", status_code=201)
async def setup_initial(
    body: InitialSetupBody,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not await _needs_initial_setup(session):
        raise HTTPException(
            status_code=403,
            detail=(
                "initial setup is not available — already completed, or set "
                "FREEAI_ADMIN_TOKEN / data/admin_token"
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
    log.info("initial_setup_completed")
    return {
        "ok": True,
        "detail": (
            "Token de administrador guardado (hash en base de datos). "
            "Las claves de proveedor se almacenan cifradas como siempre. "
            "No volveremos a mostrar el token — cópialo ahora."
        ),
    }


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
    client_hash = client.key_hash if client else None

    if req.stream:
        async def event_stream():
            try:
                async for chunk in orch.stream(
                    req, config_repo, rate_repo, usage_repo, strategy_repo,
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
            req, config_repo, rate_repo, usage_repo, strategy_repo,
            client_hash=client_hash,
        )
    except ProviderError as e:
        raise _http_from_provider_error(e)


# ──────────────────────────── provider admin ────────────────────────────


class ProviderUpdate(BaseModel):
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    weight: Optional[float] = None
    default_model: Optional[str] = None
    rpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    tags: Optional[list[str]] = None


class ProviderPatchResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    provider: ProviderStatus
    model_warning: Optional[str] = None
    model_suggestions: list[str] = Field(default_factory=list)


async def _provider_status(
    name: str, config_repo: ConfigRepository, rate_repo: RateRepository
) -> ProviderStatus:
    dto = await config_repo.get_provider(name)
    if not dto:
        raise HTTPException(404, f"unknown provider '{name}'")
    snap = await rate_repo.snapshot(name)
    return ProviderStatus(
        name=name,
        enabled=dto.enabled,
        has_key=bool(dto.api_key),
        healthy=snap.healthy,
        requests_today=snap.requests_today,
        requests_this_minute=snap.requests_this_minute,
        rpm_limit=dto.rpm_limit,
        rpd_limit=dto.rpd_limit,
        last_error=snap.last_error,
        last_latency_ms=snap.last_latency_ms,
        tags=dto.tags,
        default_model=dto.default_model,
    )


@app.get("/api/providers", response_model=list[ProviderStatus])
async def list_providers(
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> list[ProviderStatus]:
    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    providers = await config_repo.list_providers()
    return [await _provider_status(p.name, config_repo, rate_repo) for p in providers]


@app.patch("/api/providers/{name}", response_model=ProviderPatchResponse)
async def update_provider(
    name: str,
    patch: ProviderUpdate,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
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
        await rate_repo.reset_health(name)

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

    status = await _provider_status(name, config_repo, rate_repo)
    return ProviderPatchResponse(
        provider=status,
        model_warning=model_warning,
        model_suggestions=suggestions,
    )


@app.post("/api/providers/{name}/reset")
async def reset_provider_health(
    name: str,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> dict:
    rate_repo = RateRepository(session)
    await rate_repo.reset_health(name)
    return {"ok": True}


@app.get("/api/providers/{name}/models")
async def list_provider_models(
    name: str,
    _admin=Depends(require_admin),
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
    _admin=Depends(require_admin),
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
    _admin=Depends(require_admin),
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
    _admin=Depends(require_admin),
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
    _admin=Depends(require_admin),
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

    snapshots = await rate_repo.snapshot_all([p.name for p in eligible])

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
            requests_today=snap.requests_today,
            requests_this_minute=snap.requests_this_minute,
            rpd_limit=dto.rpd_limit,
            rpm_limit=dto.rpm_limit,
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
    _admin=Depends(require_admin),
) -> dict:
    """Aggregated usage summary. `window_seconds` and `bucket_count` let the
    frontend switch between "last hour / 12 buckets" and "last 24h / 24 buckets"
    etc."""
    if window_seconds < 60 or window_seconds > 7 * 24 * 3600:
        raise HTTPException(400, "window_seconds must be between 60 and 604800")
    if bucket_count < 1 or bucket_count > 168:
        raise HTTPException(400, "bucket_count must be between 1 and 168")
    repo = UsageRepository(session)
    summary = await repo.summary(window_seconds=window_seconds, bucket_count=bucket_count)
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
    _admin=Depends(require_admin),
) -> list[dict]:
    repo = ClientRepository(session)
    return [
        {"name": c.name, "key_hash": c.key_hash, "rpm_limit": c.rpm_limit, "enabled": c.enabled}
        for c in await repo.list_all()
    ]


@app.post("/api/clients", response_model=ClientCreated, status_code=201)
async def create_client(
    payload: ClientCreate,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> ClientCreated:
    repo = ClientRepository(session)
    client, raw = await repo.create(payload.name, payload.rpm_limit)
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
    _admin=Depends(require_admin),
) -> dict:
    repo = ClientRepository(session)
    if not await repo.revoke(key_hash):
        raise HTTPException(status_code=404, detail="client not found")
    return {"ok": True}


# ──────────────────────────── health + metrics ────────────────────────────


@app.get("/api/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    config_repo = ConfigRepository(session)
    client_repo = ClientRepository(session)
    providers = await config_repo.list_providers()
    return {
        "status": "ok",
        "providers_configured": sum(1 for p in providers if p.api_key),
        "clients_configured": len(await client_repo.list_all()),
        "auth_required": await client_repo.has_any(),
    }


@app.get("/metrics")
async def metrics() -> Response:
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)


# ──────────────────────────── static frontend ────────────────────────────

_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")

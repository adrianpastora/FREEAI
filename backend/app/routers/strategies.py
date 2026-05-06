"""Strategy CRUD + preview + tag vocabulary.

Two routers exported (``router`` for /api/strategies, ``tags_router`` for
/api/tags) — kept in one module because the tags endpoint exists to feed
the strategy editor's autocomplete. Splitting them by URL prefix would
require two files for one piece of UX.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from .. import strategy_dsl
from ..auth import CurrentUser
from ..db import get_session
from ..repositories import (
    ConfigRepository,
    RateRepository,
    StrategyDTO,
    StrategyRepository,
)
from ..security import get_current_user, require_admin, require_admin_user
from ..strategy_dsl import ParseError, parse_definition

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
tags_router = APIRouter(prefix="/api/tags", tags=["tags"])


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


class TagInfo(BaseModel):
    tag: str
    providers: list[str]


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


def _strategy_to_out(dto: StrategyDTO) -> StrategyOut:
    return StrategyOut(
        name=dto.name,
        definition=dto.definition,
        description=dto.description,
        is_builtin=dto.is_builtin,
    )


def _validate_definition_or_422(definition: Optional[dict]) -> None:
    """Run the DSL parser; on failure raise 422 with the parser message."""
    try:
        parse_definition(definition)
    except ParseError as e:
        raise HTTPException(422, str(e)) from e


@router.get("", response_model=list[StrategyOut])
async def list_strategies(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
) -> list[StrategyOut]:
    repo = StrategyRepository(session)
    return [_strategy_to_out(s) for s in await repo.list_all()]


@router.post("", response_model=StrategyOut)
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


@router.patch("/{name}", response_model=StrategyOut)
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


@router.delete("/{name}")
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
        raise HTTPException(400, str(e)) from e
    if not deleted:
        raise HTTPException(404, f"unknown strategy '{name}'")
    request.app.state.orchestrator.invalidate_strategy_cache(name)
    return {"ok": True}


@router.post("/preview", response_model=StrategyPreviewOut)
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
        raise HTTPException(422, str(e)) from e

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

        ctx = strategy_dsl.context_from_provider(
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
        contribution = strategy_dsl.score(defn, ctx)
        if contribution is None:
            excluded.append(dto.name)
            continue
        baseline = strategy_dsl.baseline_score(ctx)
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


@tags_router.get("", response_model=list[TagInfo])
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

"""Provider admin endpoints — /api/providers/*. Catalog-level (admin only)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..db import get_session
from ..providers.known_models import KNOWN_MODELS, is_known, suggest_similar
from ..repositories import ConfigRepository, RateRepository
from ..schemas import ProviderStatus
from ..security import get_current_user, require_admin_user

router = APIRouter(prefix="/api/providers", tags=["providers-admin"])


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


@router.get("", response_model=list[ProviderStatus])
async def list_providers(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_admin_user),
) -> list[ProviderStatus]:
    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    providers = await config_repo.list_providers()
    return [await _provider_status(p.name, config_repo, rate_repo, user.id) for p in providers]


@router.patch("/{name}", response_model=ProviderPatchResponse)
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
        raise HTTPException(404, str(e)) from e
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


@router.post("/{name}/reset")
async def reset_provider_health(
    name: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_admin_user),
) -> dict:
    rate_repo = RateRepository(session)
    await rate_repo.reset_health(user.id, name)
    return {"ok": True}


@router.get("/{name}/models")
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

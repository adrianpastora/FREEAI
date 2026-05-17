"""Pricing admin endpoints — /api/pricing/*.

Admin-only surface for inspecting and tuning ``model_prices``. The
orchestrator freezes each call's cost into ``usage_events.cost_usd`` at
write time, so edits here apply only to *future* dispatches — history is
intentionally immutable to keep analytics auditable.

Prices are in USD per **million** tokens to match every provider's
published price-list unit; the orchestrator divides by 1e6 at compute
time. Setting both fields to 0 marks a model as free-tier (distinct from
"unknown price", which is the absence of a row).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..db import get_session
from ..repositories import ModelPriceDTO, PricingRepository
from ..security import require_admin_user

router = APIRouter(prefix="/api/pricing", tags=["pricing-admin"])


class ModelPriceResponse(BaseModel):
    """One row of the ``model_prices`` table.

    ``input_per_million_usd`` and ``output_per_million_usd`` are floats to
    permit fractional cents (e.g. Cohere ``command-r7b`` at $0.0375 in).
    """
    model_config = ConfigDict(protected_namespaces=())

    provider: str
    model: str
    input_per_million_usd: float
    output_per_million_usd: float
    currency: str = "USD"
    updated_at: float


class ModelPriceUpsert(BaseModel):
    """PUT body — both prices are required so a partial update can't leave
    the row in a half-known state. Use 0 explicitly to mark free."""
    model_config = ConfigDict(protected_namespaces=())

    input_per_million_usd: float = Field(ge=0.0)
    output_per_million_usd: float = Field(ge=0.0)
    currency: str = "USD"


def _to_response(dto: ModelPriceDTO) -> ModelPriceResponse:
    return ModelPriceResponse(
        provider=dto.provider,
        model=dto.model,
        input_per_million_usd=dto.input_per_million_usd,
        output_per_million_usd=dto.output_per_million_usd,
        currency=dto.currency,
        updated_at=dto.updated_at,
    )


@router.get("", response_model=list[ModelPriceResponse])
async def list_prices(
    provider: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_admin_user),
) -> list[ModelPriceResponse]:
    """List all price rows, optionally filtered to one provider."""
    repo = PricingRepository(session)
    rows = (
        await repo.list_for_provider(provider)
        if provider else await repo.list_all()
    )
    return [_to_response(r) for r in rows]


@router.get("/{provider}/{model:path}", response_model=ModelPriceResponse)
async def get_price(
    provider: str,
    model: str,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_admin_user),
) -> ModelPriceResponse:
    """Fetch one row. 404 when the (provider, model) pair has no price set
    — the orchestrator treats that as ``cost_usd=NULL`` on future calls."""
    repo = PricingRepository(session)
    dto = await repo.get(provider, model)
    if dto is None:
        raise HTTPException(404, f"no price row for {provider}/{model}")
    return _to_response(dto)


@router.put("/{provider}/{model:path}", response_model=ModelPriceResponse)
async def upsert_price(
    provider: str,
    model: str,
    body: ModelPriceUpsert,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_admin_user),
) -> ModelPriceResponse:
    """Create or update a price row.

    Takes effect on the next dispatch for this worker immediately and on
    other workers after the in-process cache TTL (60 s). Historical
    ``usage_events.cost_usd`` rows are not rewritten.
    """
    repo = PricingRepository(session)
    dto = await repo.upsert(
        ModelPriceDTO(
            provider=provider,
            model=model,
            input_per_million_usd=body.input_per_million_usd,
            output_per_million_usd=body.output_per_million_usd,
            currency=body.currency,
        )
    )
    await session.commit()
    return _to_response(dto)


@router.delete("/{provider}/{model:path}")
async def delete_price(
    provider: str,
    model: str,
    session: AsyncSession = Depends(get_session),
    _: CurrentUser = Depends(require_admin_user),
) -> dict[str, bool]:
    """Drop a price row. Future dispatches for this (provider, model)
    will record ``cost_usd=NULL`` until a new row is inserted."""
    repo = PricingRepository(session)
    deleted = await repo.delete(provider, model)
    if not deleted:
        raise HTTPException(404, f"no price row for {provider}/{model}")
    await session.commit()
    return {"deleted": True}

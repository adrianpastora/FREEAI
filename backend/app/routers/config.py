"""App-level config endpoints — /api/config/*."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..db import get_session
from ..providers import PROVIDER_REGISTRY
from ..repositories import ConfigRepository, StrategyRepository
from ..security import get_current_user, require_admin

router = APIRouter(prefix="/api/config", tags=["config"])


class StrategyUpdate(BaseModel):
    default_strategy: str  # not a Literal anymore — custom strategies allowed


class FallbackUpdate(BaseModel):
    enable_fallback: bool


@router.get("")
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


@router.put("/strategy")
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


@router.put("/fallback")
async def set_fallback(
    payload: FallbackUpdate,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(require_admin),
) -> dict:
    repo = ConfigRepository(session)
    await repo.set_fallback(payload.enable_fallback)
    return {"enable_fallback": payload.enable_fallback}

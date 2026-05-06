"""Analytics endpoints — /api/analytics + /api/analytics/historical."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..db import get_session
from ..repositories import UsageRepository
from ..security import get_current_user

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("")
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


@router.get("/historical")
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

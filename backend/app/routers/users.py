"""User management endpoints (admin) — list / delete / reset-password / analytics."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser, hash_password
from ..db import get_session
from ..repositories import RefreshTokenRepository, UserRepository
from ..security import require_admin_user

router = APIRouter(prefix="/api/users", tags=["users"])


class ResetPasswordBody(BaseModel):
    password: str = Field(..., min_length=8, max_length=512)


@router.get("")
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


@router.delete("/{user_id}")
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


@router.post("/{user_id}/reset-password")
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


@router.get("/analytics")
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
    import time

    if days < 1 or days > 30:
        raise HTTPException(400, "days must be between 1 and 30")

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
            {"day": (datetime.fromtimestamp(idx * day_seconds, tz=timezone.utc)
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

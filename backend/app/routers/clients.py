"""Per-user API client management — /api/clients/*."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..db import get_session
from ..repositories import ClientRepository
from ..security import get_current_user

router = APIRouter(prefix="/api/clients", tags=["clients"])


class ClientCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    rpm_limit: int = Field(default=60, ge=1, le=10_000)


class ClientCreated(BaseModel):
    name: str
    api_key: str
    key_hash: str
    rpm_limit: int


@router.get("")
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


@router.post("", response_model=ClientCreated, status_code=201)
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


@router.delete("/{key_hash}")
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

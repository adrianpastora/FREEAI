"""Per-user provider credential management — /api/me/providers/*."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import CurrentUser
from ..crypto import (
    MASTER_KEY_PATH,
    MasterKeyNotReadyError,
    decrypt,
    master_key_confirmation_required,
)
from ..db import get_session
from ..db.models import ProviderConfigRow, UserProviderRow
from ..logging_config import get_logger
from ..repositories.user_provider_repo import UserProviderRepository
from ..security import get_current_user
from ..settings import get_settings

router = APIRouter(prefix="/api/me/providers", tags=["me-providers"])
log = get_logger("freeai.me_providers")


class UserProviderUpdate(BaseModel):
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    rpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    tpd_limit: Optional[int] = None
    weight: Optional[float] = None
    default_model: Optional[str] = None
    max_retries: Optional[int] = None


@router.get("")
async def list_my_providers(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    """List the current user's provider configs (keys masked)."""
    repo = UserProviderRepository(session)
    dtos = await repo.list_for_user(user.id)
    log.info(
        "list_my_providers",
        user_id=user.id, username=user.username, role=user.role,
        providers_found=len(dtos),
        with_key=sum(1 for d in dtos if d.api_key),
    )
    return [repo.mask_dto(d) for d in dtos]


@router.get("/debug")
async def debug_my_providers(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Debug endpoint — shows where keys actually are and if decrypt works."""
    # Count user_providers per user_id
    up_counts = (await session.execute(
        select(UserProviderRow.user_id, func.count()).group_by(UserProviderRow.user_id)
    )).all()
    # Count catalog keys
    catalog_keys = (await session.execute(
        select(func.count()).select_from(ProviderConfigRow)
        .where(ProviderConfigRow.api_key_encrypted.isnot(None))
    )).scalar_one()
    # This user's providers with decrypt check
    my_rows = (await session.execute(
        select(UserProviderRow)
        .where(UserProviderRow.user_id == user.id)
    )).scalars().all()
    providers_detail = []
    for r in my_rows:
        has_encrypted = r.api_key_encrypted is not None
        try:
            decrypted = decrypt(r.api_key_encrypted) if has_encrypted else None
        except MasterKeyNotReadyError:
            decrypted = None
        providers_detail.append({
            "name": r.provider_name,
            "has_encrypted_value": has_encrypted,
            "decrypt_ok": decrypted is not None,
            "has_key": bool(decrypted),
        })
    # Master key info
    if os.environ.get("FREEAI_MASTER_KEY"):
        master_key_source = "env"
    elif MASTER_KEY_PATH.exists():
        master_key_source = "file"
    elif master_key_confirmation_required():
        master_key_source = "pending-confirmation"
    else:
        master_key_source = "none"
    return {
        "your_user_id": user.id,
        "your_username": user.username,
        "your_role": user.role,
        "user_providers_by_user": {str(uid): cnt for uid, cnt in up_counts},
        "catalog_keys_remaining": catalog_keys,
        "your_providers": providers_detail,
        "master_key_source": master_key_source,
        "master_key_path": str(MASTER_KEY_PATH),
        "cors_origins": get_settings().cors_origin_list,
    }


@router.get("/catalog")
async def provider_catalog(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    """List all available providers (catalog) without keys."""
    repo = UserProviderRepository(session)
    return await repo.list_catalog()


@router.patch("/{name}")
async def update_my_provider(
    name: str,
    patch: UserProviderUpdate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    """Upsert the current user's credentials/config for a provider."""
    repo = UserProviderRepository(session)
    fields = patch.model_dump(exclude_unset=True)
    if "api_key" in fields and fields["api_key"] == "":
        fields["api_key"] = None
    try:
        dto = await repo.upsert(user.id, name, **fields)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return repo.mask_dto(dto)


@router.delete("/{name}")
async def delete_my_provider(
    name: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    repo = UserProviderRepository(session)
    if not await repo.delete(user.id, name):
        raise HTTPException(404, f"no configuration for provider '{name}'")
    return {"ok": True}

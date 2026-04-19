"""Per-user provider credentials repository.

Each user configures their own API keys / overrides for providers.
The global `providers` table acts as a catalog with defaults; user-specific
values in `user_providers` take precedence (COALESCE pattern).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt, encrypt, mask_key
from ..db.models import ProviderConfigRow, UserProviderRow


@dataclass
class UserProviderDTO:
    user_id: int
    provider_name: str
    api_key: Optional[str]         # decrypted plaintext (None = not configured)
    enabled: bool
    rpm_limit: Optional[int]       # effective (user override or catalog default)
    rpd_limit: Optional[int]
    tpd_limit: Optional[int]
    weight: float
    tags: list[str] = field(default_factory=list)  # always from catalog
    default_model: Optional[str] = None
    # None = defer to AppConfigDTO.provider_max_retries at call time.
    max_retries: Optional[int] = None


class UserProviderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def list_for_user(self, user_id: int) -> list[UserProviderDTO]:
        """Return all providers the user has configured (with catalog defaults merged)."""
        stmt = (
            select(UserProviderRow, ProviderConfigRow)
            .join(ProviderConfigRow, UserProviderRow.provider_name == ProviderConfigRow.name)
            .where(UserProviderRow.user_id == user_id)
            .order_by(UserProviderRow.provider_name)
        )
        rows = (await self._s.execute(stmt)).all()
        return [self._merge(up, cat) for up, cat in rows]

    async def list_catalog(self) -> list[dict]:
        """Return the provider catalog (no keys) for display."""
        stmt = select(ProviderConfigRow).order_by(ProviderConfigRow.name)
        rows = (await self._s.execute(stmt)).scalars().all()
        return [
            {
                "name": r.name,
                "enabled": r.enabled,
                "rpm_limit": r.rpm_limit,
                "rpd_limit": r.rpd_limit,
                "tpd_limit": r.tpd_limit,
                "weight": r.weight,
                "tags": r.tags or [],
                "default_model": r.default_model,
            }
            for r in rows
        ]

    async def get(self, user_id: int, provider_name: str) -> Optional[UserProviderDTO]:
        stmt = (
            select(UserProviderRow, ProviderConfigRow)
            .join(ProviderConfigRow, UserProviderRow.provider_name == ProviderConfigRow.name)
            .where(
                UserProviderRow.user_id == user_id,
                UserProviderRow.provider_name == provider_name,
            )
        )
        row = (await self._s.execute(stmt)).one_or_none()
        if not row:
            return None
        return self._merge(row[0], row[1])

    async def upsert(
        self,
        user_id: int,
        provider_name: str,
        *,
        api_key: Optional[str] = None,
        enabled: Optional[bool] = None,
        rpm_limit: Optional[int] = None,
        rpd_limit: Optional[int] = None,
        tpd_limit: Optional[int] = None,
        weight: Optional[float] = None,
        default_model: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> UserProviderDTO:
        # Check catalog exists
        catalog = await self._s.get(ProviderConfigRow, provider_name)
        if not catalog:
            raise KeyError(f"unknown provider '{provider_name}'")

        stmt = select(UserProviderRow).where(
            UserProviderRow.user_id == user_id,
            UserProviderRow.provider_name == provider_name,
        )
        existing = (await self._s.execute(stmt)).scalar_one_or_none()

        if existing:
            if api_key is not None:
                existing.api_key_encrypted = encrypt(api_key) if api_key else None
            if enabled is not None:
                existing.enabled = enabled
            if rpm_limit is not None:
                existing.rpm_limit = rpm_limit
            if rpd_limit is not None:
                existing.rpd_limit = rpd_limit
            if tpd_limit is not None:
                existing.tpd_limit = tpd_limit
            if weight is not None:
                existing.weight = weight
            if default_model is not None:
                existing.default_model = default_model if default_model else None
            if max_retries is not None:
                existing.max_retries = max_retries
            existing.updated_at = time.time()
            await self._s.flush()
            return self._merge(existing, catalog)

        row = UserProviderRow(
            user_id=user_id,
            provider_name=provider_name,
            api_key_encrypted=encrypt(api_key) if api_key else None,
            enabled=enabled if enabled is not None else True,
            rpm_limit=rpm_limit,
            rpd_limit=rpd_limit,
            tpd_limit=tpd_limit,
            weight=weight,
            default_model=default_model if default_model else None,
            max_retries=max_retries,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._s.add(row)
        await self._s.flush()
        return self._merge(row, catalog)

    async def delete(self, user_id: int, provider_name: str) -> bool:
        stmt = delete(UserProviderRow).where(
            UserProviderRow.user_id == user_id,
            UserProviderRow.provider_name == provider_name,
        )
        result = await self._s.execute(stmt)
        return result.rowcount > 0

    def _merge(self, up: UserProviderRow, cat: ProviderConfigRow) -> UserProviderDTO:
        """Merge user overrides with catalog defaults (user takes precedence)."""
        return UserProviderDTO(
            user_id=up.user_id,
            provider_name=up.provider_name,
            api_key=decrypt(up.api_key_encrypted),
            enabled=up.enabled,
            rpm_limit=up.rpm_limit if up.rpm_limit is not None else cat.rpm_limit,
            rpd_limit=up.rpd_limit if up.rpd_limit is not None else cat.rpd_limit,
            tpd_limit=up.tpd_limit if up.tpd_limit is not None else cat.tpd_limit,
            weight=up.weight if up.weight is not None else cat.weight,
            tags=cat.tags or [],
            default_model=up.default_model or cat.default_model,
            max_retries=up.max_retries,
        )

    @staticmethod
    def mask_dto(dto: UserProviderDTO) -> dict:
        """Safe serialization for API responses (key masked)."""
        return {
            "provider_name": dto.provider_name,
            "has_key": bool(dto.api_key),
            "key_preview": mask_key(dto.api_key),
            "enabled": dto.enabled,
            "rpm_limit": dto.rpm_limit,
            "rpd_limit": dto.rpd_limit,
            "tpd_limit": dto.tpd_limit,
            "weight": dto.weight,
            "tags": dto.tags,
            "default_model": dto.default_model,
            "max_retries": dto.max_retries,
        }

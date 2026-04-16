"""Config repository — providers + app-level settings.

Plays the role the old in-memory ConfigStore had, but reads/writes Postgres.
DTOs are plain dataclasses (not ORM rows) so the rest of the app doesn't have
to care about session lifetimes. The repository handles encrypt/decrypt at the
boundary — anything outside this file sees plaintext.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt, encrypt
from ..db.models import AppConfigRow, ProviderConfigRow


@dataclass
class ProviderConfigDTO:
    name: str
    enabled: bool = True
    api_key: Optional[str] = None  # plaintext after read; encrypted on write
    rpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    tpd_limit: Optional[int] = None
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)
    default_model: Optional[str] = None


@dataclass
class AppConfigDTO:
    default_strategy: str = "auto"
    enable_fallback: bool = True


# Defaults — used to seed an empty database on first run.
# Limits reflect each provider's free tier as of 2026-04:
#   Groq free:       30 RPM, 14 400 RPD, 500k TPD (llama-3.3-70b)
#   Gemini free:     10 RPM,   250 RPD (Flash), ~unlimited TPD at 250k TPM
#   Mistral exper:    2 RPM, ~unlimited RPD, ~1B tokens/month ≈ 33M/day
#   OpenRouter free: 20 RPM,   200 RPD (per model), no TPD
#   Cohere trial:    20 RPM, 1 000/month ≈ 33/day, no TPD
#   HuggingFace:     ~30 RPM, ~1 000 RPD (varies), no TPD
DEFAULT_PROVIDERS: dict[str, ProviderConfigDTO] = {
    "groq": ProviderConfigDTO(
        name="groq",
        rpm_limit=30, rpd_limit=14_400, tpd_limit=500_000, weight=1.0,
        tags=["fast", "cheap", "coding", "reasoning", "audio"],
        default_model="llama-3.3-70b-versatile",
    ),
    "gemini": ProviderConfigDTO(
        name="gemini",
        rpm_limit=10, rpd_limit=250, tpd_limit=None, weight=0.9,
        tags=["quality", "vision", "long_context", "reasoning", "embeddings"],
        default_model="gemini-2.5-flash",
    ),
    "mistral": ProviderConfigDTO(
        name="mistral",
        rpm_limit=2, rpd_limit=1_000_000_000, tpd_limit=33_000_000, weight=0.8,
        tags=["coding", "fast", "cheap", "embeddings"],
        default_model="mistral-small-latest",
    ),
    "openrouter": ProviderConfigDTO(
        name="openrouter",
        rpm_limit=20, rpd_limit=200, tpd_limit=None, weight=0.7,
        tags=["quality", "variety", "reasoning"],
        default_model="meta-llama/llama-3.3-70b-instruct:free",
    ),
    "cohere": ProviderConfigDTO(
        name="cohere",
        rpm_limit=20, rpd_limit=33, tpd_limit=None, weight=0.6,
        tags=["fast", "rag"],
        default_model="command-r-08-2024",
    ),
    "huggingface": ProviderConfigDTO(
        name="huggingface",
        rpm_limit=30, rpd_limit=1_000, tpd_limit=None, weight=0.5,
        tags=["variety", "cheap"],
        default_model="meta-llama/Llama-3.2-3B-Instruct",
    ),
}


class ConfigRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ──────────────── providers ────────────────

    async def list_providers(self) -> list[ProviderConfigDTO]:
        result = await self.session.execute(select(ProviderConfigRow).order_by(ProviderConfigRow.name))
        rows = result.scalars().all()
        return [self._row_to_dto(r) for r in rows]

    async def get_provider(self, name: str) -> Optional[ProviderConfigDTO]:
        row = await self.session.get(ProviderConfigRow, name)
        return self._row_to_dto(row) if row else None

    async def upsert_provider(self, dto: ProviderConfigDTO) -> ProviderConfigDTO:
        encrypted = encrypt(dto.api_key) if dto.api_key else None
        stmt = pg_insert(ProviderConfigRow).values(
            name=dto.name,
            enabled=dto.enabled,
            api_key_encrypted=encrypted,
            rpm_limit=dto.rpm_limit,
            rpd_limit=dto.rpd_limit,
            tpd_limit=dto.tpd_limit,
            weight=dto.weight,
            tags=dto.tags,
            default_model=dto.default_model,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ProviderConfigRow.name],
            set_={
                "enabled": stmt.excluded.enabled,
                "api_key_encrypted": stmt.excluded.api_key_encrypted,
                "rpm_limit": stmt.excluded.rpm_limit,
                "rpd_limit": stmt.excluded.rpd_limit,
                "tpd_limit": stmt.excluded.tpd_limit,
                "weight": stmt.excluded.weight,
                "tags": stmt.excluded.tags,
                "default_model": stmt.excluded.default_model,
            },
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return dto

    async def patch_provider(self, name: str, **fields) -> ProviderConfigDTO:
        existing = await self.get_provider(name)
        if not existing:
            raise KeyError(f"unknown provider '{name}'")
        # api_key gets encrypted at upsert time, so set plaintext here
        for k, v in fields.items():
            if hasattr(existing, k):
                setattr(existing, k, v)
        return await self.upsert_provider(existing)

    async def seed_defaults_if_empty(self) -> int:
        """Insert default providers + a single app_config row if missing.
        Returns the number of providers added."""
        result = await self.session.execute(select(ProviderConfigRow.name))
        existing = {row[0] for row in result.all()}
        added = 0
        for name, dto in DEFAULT_PROVIDERS.items():
            if name in existing:
                continue
            await self.upsert_provider(dto)
            added += 1
        # Ensure app_config exists
        cfg_row = await self.session.get(AppConfigRow, 1)
        if not cfg_row:
            self.session.add(AppConfigRow(id=1))
        await self.session.flush()
        return added

    @staticmethod
    def _row_to_dto(row: ProviderConfigRow) -> ProviderConfigDTO:
        return ProviderConfigDTO(
            name=row.name,
            enabled=row.enabled,
            api_key=decrypt(row.api_key_encrypted),
            rpm_limit=row.rpm_limit,
            rpd_limit=row.rpd_limit,
            tpd_limit=row.tpd_limit,
            weight=row.weight,
            tags=list(row.tags or []),
            default_model=row.default_model,
        )

    # ──────────────── app config ────────────────

    async def get_app_config(self) -> AppConfigDTO:
        row = await self.session.get(AppConfigRow, 1)
        if not row:
            row = AppConfigRow(id=1)
            self.session.add(row)
            await self.session.flush()
        return AppConfigDTO(
            default_strategy=row.default_strategy,
            enable_fallback=row.enable_fallback,
        )

    async def set_strategy(self, strategy: str) -> None:
        await self.session.execute(
            update(AppConfigRow).where(AppConfigRow.id == 1).values(default_strategy=strategy)
        )

    async def set_fallback(self, enabled: bool) -> None:
        await self.session.execute(
            update(AppConfigRow).where(AppConfigRow.id == 1).values(enable_fallback=enabled)
        )

    async def count_providers_with_stored_keys(self) -> int:
        r = await self.session.execute(
            select(func.count())
            .select_from(ProviderConfigRow)
            .where(ProviderConfigRow.api_key_encrypted.isnot(None))
        )
        return int(r.scalar_one())

    async def set_admin_token_hash(self, token_hash: str) -> None:
        await self.session.execute(
            update(AppConfigRow)
            .where(AppConfigRow.id == 1)
            .values(admin_token_hash=token_hash)
        )

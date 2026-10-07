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
from ..db.models import AppConfigRow, ModelPriceRow, ProviderConfigRow, UserProviderRow
from ..providers.catalog import CATALOG, CATALOG_VERSION


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
    # User-scoped override for retry budget. None = use AppConfigDTO.provider_max_retries.
    max_retries: Optional[int] = None


@dataclass
class AppConfigDTO:
    default_strategy: str = "auto"
    enable_fallback: bool = True
    provider_max_retries: int = 1
    stream_idle_timeout_s: float = 45.0
    circuit_breaker_threshold: int = 3
    circuit_breaker_window_s: int = 300
    circuit_breaker_base_cooldown_s: int = 30
    circuit_breaker_max_cooldown_s: int = 3600


# Defaults — used to seed an empty database on first run and re-applied by
# sync_catalog() whenever catalog.CATALOG_VERSION changes. Edit the catalog
# (app/providers/catalog.py), not this mapping.
DEFAULT_PROVIDERS: dict[str, ProviderConfigDTO] = {
    name: ProviderConfigDTO(
        name=name,
        rpm_limit=entry.rpm_limit,
        rpd_limit=entry.rpd_limit,
        tpd_limit=entry.tpd_limit,
        weight=entry.weight,
        tags=list(entry.tags),
        default_model=entry.default_model,
    )
    for name, entry in CATALOG.items()
}


@dataclass
class CatalogSyncResult:
    applied: bool
    version: str
    providers_added: list[str] = field(default_factory=list)
    providers_updated: list[str] = field(default_factory=list)
    prices_added: int = 0
    user_overrides_cleared: int = 0


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

    async def sync_catalog(self) -> CatalogSyncResult:
        """Push catalog defaults into the database when CATALOG_VERSION changed.

        On a version bump, for every built-in provider:
          • missing catalog rows are inserted;
          • default_model / limits / weight / tags are overwritten with the
            catalog values (enabled flag and stored key are left alone);
          • user overrides of default_model that point at a retired model
            are cleared so the user falls back to the catalog default;
          • catalog price hints are inserted for models without a price
            (ON CONFLICT DO NOTHING keeps admin-tuned prices).
        Admin edits to catalog rows therefore survive until the next bump.
        """
        cfg_row = await self.session.get(AppConfigRow, 1)
        if not cfg_row:
            cfg_row = AppConfigRow(id=1)
            self.session.add(cfg_row)
            await self.session.flush()
        result = CatalogSyncResult(applied=False, version=CATALOG_VERSION)
        if cfg_row.catalog_version == CATALOG_VERSION:
            return result
        result.applied = True

        rows = (await self.session.execute(select(ProviderConfigRow))).scalars().all()
        by_name = {r.name: r for r in rows}
        for name, entry in CATALOG.items():
            row = by_name.get(name)
            if row is None:
                await self.upsert_provider(DEFAULT_PROVIDERS[name])
                result.providers_added.append(name)
                continue
            new_values = {
                "default_model": entry.default_model,
                "rpm_limit": entry.rpm_limit,
                "rpd_limit": entry.rpd_limit,
                "tpd_limit": entry.tpd_limit,
                "weight": entry.weight,
                "tags": list(entry.tags),
            }
            if any(getattr(row, k) != v for k, v in new_values.items()):
                for k, v in new_values.items():
                    setattr(row, k, v)
                result.providers_updated.append(name)

            if entry.retired_models:
                cleared = await self.session.execute(
                    update(UserProviderRow)
                    .where(
                        UserProviderRow.provider_name == name,
                        UserProviderRow.default_model.in_(entry.retired_models),
                    )
                    .values(default_model=None)
                )
                result.user_overrides_cleared += cleared.rowcount or 0

            for m in entry.models:
                if m.price is None:
                    continue
                ins = (
                    pg_insert(ModelPriceRow)
                    .values(
                        provider_name=name, model=m.id,
                        input_price_per_million_usd=m.price[0],
                        output_price_per_million_usd=m.price[1],
                    )
                    .on_conflict_do_nothing(
                        index_elements=[ModelPriceRow.provider_name, ModelPriceRow.model],
                    )
                )
                res = await self.session.execute(ins)
                result.prices_added += res.rowcount or 0

        cfg_row.catalog_version = CATALOG_VERSION
        await self.session.flush()
        return result

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
            provider_max_retries=row.provider_max_retries,
            stream_idle_timeout_s=row.stream_idle_timeout_s,
            circuit_breaker_threshold=row.circuit_breaker_threshold,
            circuit_breaker_window_s=row.circuit_breaker_window_s,
            circuit_breaker_base_cooldown_s=row.circuit_breaker_base_cooldown_s,
            circuit_breaker_max_cooldown_s=row.circuit_breaker_max_cooldown_s,
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

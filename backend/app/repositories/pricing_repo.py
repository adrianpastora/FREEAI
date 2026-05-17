"""Model price repository — looks up (provider, model) → USD per-million rates.

Cost is frozen onto the ``usage_events.cost_usd`` column at write time so
later edits to ``model_prices`` don't rewrite historical analytics.

The orchestrator hits this on every successful dispatch, so reads go
through a short-TTL in-process cache keyed on (provider, model). Cache
invalidation is explicit (on upsert/delete) so admin price edits take
effect immediately for the same worker; other workers will see the new
price after their TTL expires. The cache is intentionally tiny and
unbounded by an LRU — at <500 (provider, model) pairs in any realistic
deployment the memory footprint is negligible.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ModelPriceRow


@dataclass
class ModelPriceDTO:
    provider: str
    model: str
    input_per_million_usd: float
    output_per_million_usd: float
    currency: str = "USD"
    updated_at: float = 0.0

    def compute_cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Frozen cost for one dispatch. ``prompt`` and ``completion`` are
        the upstream-reported counts (or our tiktoken fallback when the
        upstream is silent — see providers.base.estimate_tokens)."""
        return (
            (prompt_tokens / 1_000_000.0) * self.input_per_million_usd
            + (completion_tokens / 1_000_000.0) * self.output_per_million_usd
        )


# Cache TTL: long enough to absorb a hot dispatch loop, short enough that
# a price edit takes effect across workers without restart. 60 s is the
# same window the orchestrator already uses for rate-limit snapshots.
_CACHE_TTL_S = 60.0


class PricingRepository:
    # Class-level cache so the lookup is shared across sessions in the same
    # worker. Keyed on (provider, model). A negative entry (None) is also
    # cached to avoid repeated DB hits for unpriced models — those are
    # expected (HuggingFace open-weight, new previews) and shouldn't trash
    # request latency by hammering the DB on every call.
    _cache: dict[tuple[str, str], tuple[Optional[ModelPriceDTO], float]] = {}

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, provider: str, model: str) -> Optional[ModelPriceDTO]:
        """Return the price row for (provider, model), or None when no row exists."""
        key = (provider, model)
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and now - cached[1] < _CACHE_TTL_S:
            return cached[0]

        row = await self.session.get(ModelPriceRow, (provider, model))
        dto = self._to_dto(row) if row else None
        self._cache[key] = (dto, now)
        return dto

    async def compute_cost_usd(
        self, provider: str, model: Optional[str],
        prompt_tokens: int, completion_tokens: int,
    ) -> Optional[float]:
        """Resolve the price and compute frozen cost in one call.

        Returns ``None`` when no price row exists for (provider, model);
        the caller writes ``NULL`` into ``usage_events.cost_usd`` rather
        than 0 so missing-price coverage stays auditable.
        """
        if not model:
            return None
        price = await self.get(provider, model)
        if price is None:
            return None
        return price.compute_cost_usd(prompt_tokens, completion_tokens)

    async def list_all(self) -> list[ModelPriceDTO]:
        result = await self.session.execute(
            select(ModelPriceRow).order_by(
                ModelPriceRow.provider_name, ModelPriceRow.model,
            )
        )
        return [self._to_dto(r) for r in result.scalars().all()]

    async def list_for_provider(self, provider: str) -> list[ModelPriceDTO]:
        result = await self.session.execute(
            select(ModelPriceRow)
            .where(ModelPriceRow.provider_name == provider)
            .order_by(ModelPriceRow.model)
        )
        return [self._to_dto(r) for r in result.scalars().all()]

    async def upsert(self, dto: ModelPriceDTO) -> ModelPriceDTO:
        stmt = pg_insert(ModelPriceRow).values(
            provider_name=dto.provider,
            model=dto.model,
            input_price_per_million_usd=dto.input_per_million_usd,
            output_price_per_million_usd=dto.output_per_million_usd,
            currency=dto.currency,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ModelPriceRow.provider_name, ModelPriceRow.model],
            set_={
                "input_price_per_million_usd": stmt.excluded.input_price_per_million_usd,
                "output_price_per_million_usd": stmt.excluded.output_price_per_million_usd,
                "currency": stmt.excluded.currency,
                "updated_at": time.time(),
            },
        )
        await self.session.execute(stmt)
        await self.session.flush()
        # Drop both positive and negative cache entries for this key so the
        # next read sees the new price (or absence) on this worker.
        self._cache.pop((dto.provider, dto.model), None)
        return dto

    async def delete(self, provider: str, model: str) -> bool:
        result = await self.session.execute(
            delete(ModelPriceRow).where(
                ModelPriceRow.provider_name == provider,
                ModelPriceRow.model == model,
            )
        )
        self._cache.pop((provider, model), None)
        return bool(result.rowcount)

    @classmethod
    def clear_cache(cls) -> None:
        """Test hook + a safety net for callers that update prices outside
        the repo (e.g. a manual SQL fixup). Costs nothing in steady state."""
        cls._cache.clear()

    @staticmethod
    def _to_dto(row: ModelPriceRow) -> ModelPriceDTO:
        return ModelPriceDTO(
            provider=row.provider_name,
            model=row.model,
            input_per_million_usd=row.input_price_per_million_usd,
            output_per_million_usd=row.output_price_per_million_usd,
            currency=row.currency,
            updated_at=row.updated_at,
        )

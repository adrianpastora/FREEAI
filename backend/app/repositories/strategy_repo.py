"""Strategy repository — routing rules stored as data.

The built-in strategies mirror what used to be STRATEGY_TAGS in orchestrator.py.
They're seeded on first run and flagged is_builtin=True so the UI/API can
distinguish user-defined ones (which can be deleted) from the canonical set
(which can only be edited).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import StrategyRow


@dataclass
class StrategyDTO:
    name: str
    tags: list[str] = field(default_factory=list)
    description: str = ""
    is_builtin: bool = False


# Must match the Strategy Literal in schemas.py for the built-in names.
BUILTIN_STRATEGIES: list[StrategyDTO] = [
    StrategyDTO("auto", [], "Reads the prompt and picks a lane.", is_builtin=True),
    StrategyDTO("fastest", ["fast"], "Lowest expected latency.", is_builtin=True),
    StrategyDTO("cheapest", ["cheap"], "Most generous free quotas.", is_builtin=True),
    StrategyDTO("best_quality", ["quality", "reasoning"], "Highest-rated reasoning models.", is_builtin=True),
    StrategyDTO("coding", ["coding", "reasoning"], "Tuned for code, tracebacks, refactors.", is_builtin=True),
    StrategyDTO("reasoning", ["reasoning", "quality"], "Multi-step thinking and explanations.", is_builtin=True),
    StrategyDTO("vision", ["vision"], "Image-capable providers only.", is_builtin=True),
    StrategyDTO("long_context", ["long_context"], "Large context windows.", is_builtin=True),
]


class StrategyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_all(self) -> list[StrategyDTO]:
        result = await self.session.execute(select(StrategyRow).order_by(StrategyRow.name))
        return [self._to_dto(r) for r in result.scalars().all()]

    async def get(self, name: str) -> Optional[StrategyDTO]:
        row = await self.session.get(StrategyRow, name)
        return self._to_dto(row) if row else None

    async def upsert(self, dto: StrategyDTO) -> StrategyDTO:
        stmt = pg_insert(StrategyRow).values(
            name=dto.name,
            tags=dto.tags,
            description=dto.description,
            is_builtin=dto.is_builtin,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[StrategyRow.name],
            set_={
                "tags": stmt.excluded.tags,
                "description": stmt.excluded.description,
                # Never overwrite is_builtin from an update — it's set once at seed
            },
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return dto

    async def delete(self, name: str) -> bool:
        existing = await self.get(name)
        if not existing:
            return False
        if existing.is_builtin:
            raise ValueError(f"strategy '{name}' is built-in and cannot be deleted")
        await self.session.execute(delete(StrategyRow).where(StrategyRow.name == name))
        return True

    async def seed_builtins_if_missing(self) -> int:
        existing = {dto.name for dto in await self.list_all()}
        added = 0
        for dto in BUILTIN_STRATEGIES:
            if dto.name in existing:
                continue
            await self.upsert(dto)
            added += 1
        return added

    @staticmethod
    def _to_dto(row: StrategyRow) -> StrategyDTO:
        return StrategyDTO(
            name=row.name,
            tags=list(row.tags or []),
            description=row.description,
            is_builtin=row.is_builtin,
        )

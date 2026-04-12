"""Strategy repository — routing rules stored as data.

Each strategy carries a DSL `definition` (see app.strategy_dsl) that the
orchestrator uses to filter and score providers. Built-in strategies are
seeded on first run with is_builtin=True so the UI/API can distinguish
them from user-defined ones (which can be deleted; built-ins can only
be edited).

The special strategy `auto` carries definition=None — it's a hardcoded
prompt-inspector that picks one of the other strategies at request time.
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
    # JSON-shaped DSL definition; None means "no DSL rules" (used by `auto`,
    # which delegates to detect_auto_strategy() at request time).
    definition: Optional[dict] = None
    description: str = ""
    is_builtin: bool = False


def _tag_prefer(tag: str, weight: float = 5.0) -> dict:
    """Shorthand for the most common DSL clause: prefer providers with this tag."""
    return {"field": "tags", "op": "contains", "value": tag, "weight": weight}


def _tag_require(tag: str) -> dict:
    """Shorthand for: provider MUST have this tag."""
    return {"field": "tags", "op": "contains", "value": tag}


# Built-in strategies, expressed as DSL definitions. Each one matches what
# the old `tags` list achieved, but now the routing intent is explicit:
# `require` filters out providers that don't qualify; `prefer` adds points.
# See docs/STRATEGY_DSL.md for the rationale behind each shape.
BUILTIN_STRATEGIES: list[StrategyDTO] = [
    StrategyDTO(
        name="auto",
        definition=None,
        description="Reads the prompt and picks a lane.",
        is_builtin=True,
    ),
    StrategyDTO(
        name="fastest",
        definition={
            "require": [],
            "prefer": [
                _tag_prefer("fast", 5),
                {"field": "latency_p50_ms", "op": "<", "value": 1000, "weight": 3},
            ],
        },
        description="Lowest expected latency.",
        is_builtin=True,
    ),
    StrategyDTO(
        name="cheapest",
        definition={
            "require": [],
            "prefer": [
                _tag_prefer("cheap", 5),
                {"field": "rpd_remaining", "op": ">", "value": 0.5, "weight": 3},
            ],
        },
        description="Most generous free quotas.",
        is_builtin=True,
    ),
    StrategyDTO(
        name="best_quality",
        definition={
            "require": [],
            "prefer": [
                _tag_prefer("quality", 5),
                _tag_prefer("reasoning", 4),
            ],
        },
        description="Highest-rated reasoning models.",
        is_builtin=True,
    ),
    StrategyDTO(
        name="coding",
        definition={
            "require": [_tag_require("coding")],
            "prefer": [
                _tag_prefer("reasoning", 5),
                {"field": "latency_p50_ms", "op": "<", "value": 2000, "weight": 2},
            ],
        },
        description="Tuned for code, tracebacks, refactors.",
        is_builtin=True,
    ),
    StrategyDTO(
        name="reasoning",
        definition={
            "require": [],
            "prefer": [
                _tag_prefer("reasoning", 5),
                _tag_prefer("quality", 3),
            ],
        },
        description="Multi-step thinking and explanations.",
        is_builtin=True,
    ),
    StrategyDTO(
        name="vision",
        definition={
            "require": [_tag_require("vision")],
            "prefer": [],
        },
        description="Image-capable providers only.",
        is_builtin=True,
    ),
    StrategyDTO(
        name="long_context",
        definition={
            "require": [_tag_require("long_context")],
            "prefer": [],
        },
        description="Large context windows.",
        is_builtin=True,
    ),
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
            definition=dto.definition,
            description=dto.description,
            is_builtin=dto.is_builtin,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[StrategyRow.name],
            set_={
                "definition": stmt.excluded.definition,
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
            definition=row.definition,  # JSONB → dict (or None for `auto`)
            description=row.description,
            is_builtin=row.is_builtin,
        )

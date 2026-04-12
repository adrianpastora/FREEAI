"""Strategy repository — seeding, upsert, delete protection for built-ins."""
from __future__ import annotations

import pytest

from app.repositories import StrategyDTO, StrategyRepository


def _coding_def() -> dict:
    """Helper: a definition that requires the coding tag."""
    return {
        "require": [{"field": "tags", "op": "contains", "value": "coding"}],
        "prefer": [],
    }


def _fast_coding_def() -> dict:
    """Helper: prefer fast + coding tags."""
    return {
        "require": [],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
            {"field": "tags", "op": "contains", "value": "coding", "weight": 3},
        ],
    }


@pytest.mark.asyncio
async def test_seed_is_idempotent(session):
    repo = StrategyRepository(session)
    first = await repo.seed_builtins_if_missing()
    await session.commit()
    second = await repo.seed_builtins_if_missing()
    await session.commit()
    assert first > 0
    assert second == 0


@pytest.mark.asyncio
async def test_seeded_strategies_are_builtin(session):
    repo = StrategyRepository(session)
    await repo.seed_builtins_if_missing()
    await session.commit()
    all_strategies = await repo.list_all()
    names = {s.name for s in all_strategies}
    assert "auto" in names
    assert "coding" in names
    # All seeded ones are builtin
    for s in all_strategies:
        assert s.is_builtin is True


@pytest.mark.asyncio
async def test_create_custom_strategy(session):
    repo = StrategyRepository(session)
    dto = StrategyDTO(
        name="my_strategy",
        definition=_fast_coding_def(),
        description="mine",
        is_builtin=False,
    )
    await repo.upsert(dto)
    await session.commit()
    found = await repo.get("my_strategy")
    assert found is not None
    assert found.definition == _fast_coding_def()
    assert found.is_builtin is False


@pytest.mark.asyncio
async def test_cannot_delete_builtin(session):
    repo = StrategyRepository(session)
    await repo.seed_builtins_if_missing()
    await session.commit()
    with pytest.raises(ValueError):
        await repo.delete("coding")


@pytest.mark.asyncio
async def test_can_delete_custom(session):
    repo = StrategyRepository(session)
    await repo.upsert(
        StrategyDTO(name="temp", definition=None, description="", is_builtin=False)
    )
    await session.commit()
    deleted = await repo.delete("temp")
    assert deleted is True
    assert await repo.get("temp") is None


@pytest.mark.asyncio
async def test_upsert_preserves_builtin_flag(session):
    """PATCH on a built-in should NOT flip is_builtin to False even if the
    caller passes is_builtin=False. The repo's upsert merges, but main.py is
    the one that carries the flag forward — this test locks the SQL-level
    semantics in place."""
    repo = StrategyRepository(session)
    # seed
    await repo.upsert(
        StrategyDTO(
            name="coding",
            definition=_coding_def(),
            description="orig",
            is_builtin=True,
        )
    )
    await session.commit()
    # update — our SQL excludes is_builtin from the SET clause
    new_def = {
        "require": [{"field": "tags", "op": "contains", "value": "coding"}],
        "prefer": [{"field": "tags", "op": "contains", "value": "reasoning", "weight": 3}],
    }
    await repo.upsert(
        StrategyDTO(
            name="coding",
            definition=new_def,
            description="new",
            is_builtin=False,
        )
    )
    await session.commit()
    found = await repo.get("coding")
    assert found.is_builtin is True
    assert found.definition == new_def
    assert found.description == "new"

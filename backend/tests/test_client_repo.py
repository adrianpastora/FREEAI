"""Client repository — issuance, lookup, revocation."""
from __future__ import annotations

import pytest

from app.repositories import ClientRepository


@pytest.mark.asyncio
async def test_no_clients_initially(session):
    repo = ClientRepository(session)
    assert await repo.has_any() is False
    assert await repo.list_all() == []


@pytest.mark.asyncio
async def test_create_and_find(session):
    repo = ClientRepository(session)
    client, raw = await repo.create("test", rpm_limit=10)
    await session.commit()

    found = await repo.find_by_raw_key(raw)
    assert found is not None
    assert found.name == "test"
    assert found.rpm_limit == 10
    assert await repo.find_by_raw_key("wrong") is None


@pytest.mark.asyncio
async def test_raw_key_not_in_db(session):
    """The raw key must NOT appear in the persisted row."""
    from sqlalchemy import select
    from app.db.models import ClientRow

    repo = ClientRepository(session)
    _, raw = await repo.create("test", rpm_limit=10)
    await session.commit()

    result = await session.execute(select(ClientRow))
    rows = result.scalars().all()
    for r in rows:
        assert raw not in r.key_hash
        assert raw not in r.name


@pytest.mark.asyncio
async def test_revoke(session):
    repo = ClientRepository(session)
    client, raw = await repo.create("doomed", rpm_limit=10)
    await session.commit()

    assert await repo.find_by_raw_key(raw) is not None
    assert await repo.revoke(client.key_hash) is True
    await session.commit()
    assert await repo.find_by_raw_key(raw) is None
    assert await repo.revoke(client.key_hash) is False

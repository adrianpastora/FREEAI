"""Client repository — issuance, lookup, revocation."""
from __future__ import annotations

import pytest

from app.repositories import ClientRepository
from app.repositories.user_repo import UserRepository


async def _admin_id(session) -> int:
    user = await UserRepository(session).find_by_username("testadmin")
    return user.id


@pytest.mark.asyncio
async def test_no_clients_initially(session):
    repo = ClientRepository(session)
    assert await repo.has_any() is False
    assert await repo.list_all() == []


@pytest.mark.asyncio
async def test_create_and_find(seeded_session):
    user_id = await _admin_id(seeded_session)
    repo = ClientRepository(seeded_session)
    client, raw = await repo.create("test", user_id=user_id, rpm_limit=10)
    await seeded_session.commit()

    found = await repo.find_by_raw_key(raw)
    assert found is not None
    assert found.name == "test"
    assert found.rpm_limit == 10
    assert await repo.find_by_raw_key("wrong") is None


@pytest.mark.asyncio
async def test_raw_key_not_in_db(seeded_session):
    """The raw key must NOT appear in the persisted row."""
    from sqlalchemy import select
    from app.db.models import ClientRow

    user_id = await _admin_id(seeded_session)
    repo = ClientRepository(seeded_session)
    _, raw = await repo.create("test", user_id=user_id, rpm_limit=10)
    await seeded_session.commit()

    result = await seeded_session.execute(select(ClientRow))
    rows = result.scalars().all()
    for r in rows:
        assert raw not in r.key_hash
        assert raw not in r.name


@pytest.mark.asyncio
async def test_revoke(seeded_session):
    user_id = await _admin_id(seeded_session)
    repo = ClientRepository(seeded_session)
    client, raw = await repo.create("doomed", user_id=user_id, rpm_limit=10)
    await seeded_session.commit()

    assert await repo.find_by_raw_key(raw) is not None
    assert await repo.revoke(client.key_hash) is True
    await seeded_session.commit()
    assert await repo.find_by_raw_key(raw) is None
    assert await repo.revoke(client.key_hash) is False

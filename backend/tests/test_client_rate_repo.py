"""Per-client rate limiting — the new dedicated table + plpgsql function.

Covers the fix for REVIEW § 1.1: previously security.py reused the provider
rate_events table via a synthetic name that violated a FK; every real call
crashed. The new path has its own table and its own function.
"""
from __future__ import annotations

import asyncio

import pytest

from app.repositories import ClientRateRepository


@pytest.mark.asyncio
async def test_try_acquire_allows_under_limit(session):
    repo = ClientRateRepository(session)
    for _ in range(3):
        assert await repo.try_acquire("abc123", rpm_limit=5) is True
        await session.commit()


@pytest.mark.asyncio
async def test_try_acquire_blocks_at_limit(session):
    repo = ClientRateRepository(session)
    for _ in range(5):
        assert await repo.try_acquire("abc123", rpm_limit=5) is True
        await session.commit()
    # the 6th request trips the cap
    assert await repo.try_acquire("abc123", rpm_limit=5) is False


@pytest.mark.asyncio
async def test_try_acquire_separates_clients(session):
    """Two clients with the same rpm cap must not interfere with each other."""
    repo = ClientRateRepository(session)
    for _ in range(5):
        assert await repo.try_acquire("alice", rpm_limit=5) is True
        await session.commit()
    # Alice is capped but Bob can still acquire
    assert await repo.try_acquire("alice", rpm_limit=5) is False
    assert await repo.try_acquire("bob", rpm_limit=5) is True


@pytest.mark.asyncio
async def test_try_acquire_no_foreign_key_violation(session):
    """The whole point of the fix: no FK to providers.name. A totally
    synthetic client hash that doesn't exist anywhere else in the schema
    must Just Work — the old code crashed here on the first real call."""
    repo = ClientRateRepository(session)
    result = await repo.try_acquire("brand_new_client_never_seen_before", rpm_limit=10)
    await session.commit()
    assert result is True


@pytest.mark.asyncio
async def test_concurrent_client_reservations_respect_limit(sessionmaker):
    """50 concurrent sessions, cap of 5 → exactly 5 succeed. This mirrors
    the providers-side test_concurrent_reservations_respect_limit and proves
    the new plpgsql function's xact advisory lock serializes correctly."""
    LIMIT = 5

    async def one():
        async with sessionmaker() as s:
            repo = ClientRateRepository(s)
            ok = await repo.try_acquire("race", rpm_limit=LIMIT)
            await s.commit()
            return ok

    results = await asyncio.gather(*[one() for _ in range(50)])
    assert sum(1 for r in results if r) == LIMIT

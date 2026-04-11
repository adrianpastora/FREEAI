"""Usage repository — record + analytics summary."""
from __future__ import annotations

import time

import pytest

from app.repositories import UsageEvent, UsageRepository


async def _record(repo: UsageRepository, *, provider: str, outcome: str, latency: int, tokens: int = 0, strategy: str = "auto"):
    await repo.record(
        UsageEvent(
            provider=provider,
            model=f"{provider}-model",
            strategy=strategy,
            outcome=outcome,
            latency_ms=latency,
            prompt_tokens=tokens // 2,
            completion_tokens=tokens - tokens // 2,
            fallback_position=1,
        )
    )


@pytest.mark.asyncio
async def test_summary_empty(session):
    repo = UsageRepository(session)
    summary = await repo.summary(window_seconds=3600, bucket_count=12)
    assert summary.total_calls == 0
    assert summary.by_provider == []
    assert len(summary.time_buckets) == 12


@pytest.mark.asyncio
async def test_summary_single_event(session):
    repo = UsageRepository(session)
    await _record(repo, provider="groq", outcome="success", latency=150, tokens=200)
    await session.commit()

    summary = await repo.summary(window_seconds=3600, bucket_count=12)
    assert summary.total_calls == 1
    assert summary.success_calls == 1
    assert summary.failed_calls == 0
    assert summary.success_rate == 1.0
    assert summary.p50_latency_ms == 150
    assert summary.total_tokens == 200
    assert len(summary.by_provider) == 1
    assert summary.by_provider[0] == {
        "provider": "groq",
        "calls": 1,
        "success": 1,
        "avg_latency_ms": 150,
        "tokens": 200,
    }


@pytest.mark.asyncio
async def test_summary_mixed_outcomes(session):
    repo = UsageRepository(session)
    for _ in range(3):
        await _record(repo, provider="groq", outcome="success", latency=100)
    await _record(repo, provider="groq", outcome="rate_limited", latency=20)
    await _record(repo, provider="gemini", outcome="success", latency=300)
    await session.commit()

    summary = await repo.summary(window_seconds=3600, bucket_count=12)
    assert summary.total_calls == 5
    assert summary.success_calls == 4
    assert summary.failed_calls == 1
    assert round(summary.success_rate, 2) == 0.8

    providers = {p["provider"]: p for p in summary.by_provider}
    assert providers["groq"]["calls"] == 4
    assert providers["groq"]["success"] == 3
    assert providers["gemini"]["calls"] == 1

    outcomes = {o["outcome"]: o["calls"] for o in summary.by_outcome}
    assert outcomes["success"] == 4
    assert outcomes["rate_limited"] == 1


@pytest.mark.asyncio
async def test_summary_window_filter(session):
    """Events outside the window should not count."""
    repo = UsageRepository(session)
    # recent
    await _record(repo, provider="groq", outcome="success", latency=100)
    await session.commit()
    # and an ancient one injected directly so we don't have to wait
    from app.db.models import UsageEventRow
    session.add(
        UsageEventRow(
            occurred_at=time.time() - 2 * 86400,
            provider_name="groq",
            model="groq-model",
            strategy="auto",
            outcome="success",
            latency_ms=999,
            prompt_tokens=0,
            completion_tokens=0,
            fallback_position=1,
        )
    )
    await session.commit()

    summary = await repo.summary(window_seconds=3600, bucket_count=12)
    assert summary.total_calls == 1  # the ancient one was filtered


@pytest.mark.asyncio
async def test_summary_time_buckets_dense(session):
    repo = UsageRepository(session)
    await _record(repo, provider="groq", outcome="success", latency=50)
    await session.commit()

    summary = await repo.summary(window_seconds=3600, bucket_count=6)
    # must return exactly 6 buckets, all present (zero-filled where empty)
    assert len(summary.time_buckets) == 6
    total = sum(b["calls"] for b in summary.time_buckets)
    assert total == 1

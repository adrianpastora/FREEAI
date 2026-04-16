"""Daily rollup + historical summary — enriched analytics with 2-year retention."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.models import UsageEventRow
from app.repositories import UsageEvent, UsageRepository


def _ts_for_day(day: date, hour: int = 12) -> float:
    dt = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=hour)
    return dt.timestamp()


async def _inject(session, *, day: date, provider: str, outcome: str,
                  latency: int, tokens: int = 0, model: str | None = None,
                  strategy: str = "auto", user_id: int = 1,
                  fallback_position: int = 1, ttfb_ms: int | None = None):
    session.add(UsageEventRow(
        occurred_at=_ts_for_day(day),
        provider_name=provider,
        model=model or f"{provider}-model",
        strategy=strategy,
        outcome=outcome,
        latency_ms=latency,
        prompt_tokens=tokens // 2,
        completion_tokens=tokens - tokens // 2,
        fallback_position=fallback_position,
        user_id=user_id,
        ttfb_ms=ttfb_ms,
    ))


@pytest.mark.asyncio
async def test_rollup_day_basic(session):
    """Rollup a single day with mixed outcomes and verify counts + percentiles."""
    repo = UsageRepository(session)
    target = date.today() - timedelta(days=1)
    # 10 successes with known latencies to test percentiles
    for lat in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        await _inject(session, day=target, provider="groq", outcome="success",
                      latency=lat, tokens=100)
    # 2 failures
    await _inject(session, day=target, provider="groq", outcome="rate_limited", latency=15)
    await _inject(session, day=target, provider="gemini", outcome="server_error", latency=500)
    await session.commit()

    rows_upserted = await repo.rollup_day(target)
    await session.commit()
    assert rows_upserted >= 2  # groq + gemini distinct rows

    result = await session.execute(text("""
        SELECT provider_name, total_calls, success_calls, failed_calls,
               p50_latency_ms, p95_latency_ms, p99_latency_ms,
               prompt_tokens, completion_tokens, errors_by_kind
        FROM usage_daily_rollup WHERE day = :d ORDER BY provider_name
    """).bindparams(d=target))
    rows = result.all()
    by_provider = {r[0]: r for r in rows}

    groq = by_provider["groq"]
    # 10 successes + 1 rate_limited
    assert groq[1] == 11
    assert groq[2] == 10
    assert groq[3] == 1
    # latency percentiles from the 11 samples (10-100 range + 15)
    assert groq[4] is not None  # p50
    assert groq[5] is not None  # p95
    assert groq[6] is not None  # p99
    # tokens: 10 success calls @ 100 tokens each = 1000 total (split prompt/completion)
    assert groq[7] + groq[8] == 1000
    # errors_by_kind is JSONB
    assert groq[9] == {"rate_limited": 1}

    gemini = by_provider["gemini"]
    assert gemini[1] == 1
    assert gemini[3] == 1
    assert gemini[9] == {"server_error": 1}


@pytest.mark.asyncio
async def test_rollup_day_idempotent(session):
    """Running rollup_day twice on the same day should replace, not duplicate."""
    repo = UsageRepository(session)
    target = date.today() - timedelta(days=1)
    await _inject(session, day=target, provider="groq", outcome="success", latency=100)
    await session.commit()

    await repo.rollup_day(target)
    await session.commit()

    # Add one more event and re-roll
    await _inject(session, day=target, provider="groq", outcome="success", latency=200)
    await session.commit()
    await repo.rollup_day(target)
    await session.commit()

    result = await session.execute(text("""
        SELECT COUNT(*), SUM(total_calls) FROM usage_daily_rollup WHERE day = :d
    """).bindparams(d=target))
    row_count, total_calls = result.one()
    assert row_count == 1  # still one row (same grouping keys)
    assert total_calls == 2  # both events counted


@pytest.mark.asyncio
async def test_rollup_fallback_histogram(session):
    """Fallback positions >=3 should collapse into bucket 3."""
    repo = UsageRepository(session)
    target = date.today() - timedelta(days=1)
    for pos in [1, 1, 1, 2, 3, 4, 5]:
        await _inject(session, day=target, provider="groq", outcome="success",
                      latency=50, fallback_position=pos)
    await session.commit()
    await repo.rollup_day(target)
    await session.commit()

    result = await session.execute(text(
        "SELECT fallback_position_hist FROM usage_daily_rollup WHERE day = :d"
    ).bindparams(d=target))
    hist = result.scalar_one()
    # positions 3, 4, 5 all collapse into bucket "3"
    assert hist == {"1": 3, "2": 1, "3": 3}


@pytest.mark.asyncio
async def test_historical_summary_dense_daily_series(session):
    """historical_summary should always produce `days` daily entries (zero-filled)."""
    repo = UsageRepository(session)
    # Seed rollups for 2 days out of the last 30
    today = date.today()
    d_recent = today - timedelta(days=2)
    d_older = today - timedelta(days=10)
    for d in (d_recent, d_older):
        await _inject(session, day=d, provider="groq", outcome="success",
                      latency=50, tokens=200)
    await session.commit()
    await repo.rollup_day(d_recent)
    await repo.rollup_day(d_older)
    await session.commit()

    hs = await repo.historical_summary(days=30, user_id=1)
    assert hs.days == 30
    assert len(hs.daily) == 30
    # Dense — all entries have day/calls/tokens keys
    assert all({"day", "calls", "success", "tokens"} <= set(d.keys()) for d in hs.daily)
    # The days we seeded have calls > 0; the others are zero-filled.
    non_zero = [d for d in hs.daily if d["calls"] > 0]
    assert len(non_zero) == 2
    assert hs.total_calls == 2

    # Provider + model aggregates present
    assert len(hs.by_provider) == 1
    assert hs.by_provider[0]["provider"] == "groq"
    assert hs.by_provider[0]["calls"] == 2
    assert len(hs.by_model) == 1


@pytest.mark.asyncio
async def test_historical_summary_scoped_by_user(session):
    """historical_summary filters by user_id."""
    repo = UsageRepository(session)
    target = date.today() - timedelta(days=1)
    await _inject(session, day=target, provider="groq", outcome="success",
                  latency=50, user_id=1)
    await _inject(session, day=target, provider="groq", outcome="success",
                  latency=50, user_id=2)
    await session.commit()
    await repo.rollup_day(target)
    await session.commit()

    hs_user1 = await repo.historical_summary(days=30, user_id=1)
    hs_user2 = await repo.historical_summary(days=30, user_id=2)
    assert hs_user1.total_calls == 1
    assert hs_user2.total_calls == 1


@pytest.mark.asyncio
async def test_purge_rollups_older_than(session):
    """purge_rollups_older_than should drop rows older than the cutoff."""
    repo = UsageRepository(session)
    old_day = date.today() - timedelta(days=800)
    recent_day = date.today() - timedelta(days=100)
    # Insert a rollup row directly — rollup_day() wouldn't find raw events
    # 800 days back anyway (usage_events gets purged at 90d).
    await session.execute(text("""
        INSERT INTO usage_daily_rollup (
            user_id, day, provider_name, model, strategy,
            total_calls, success_calls, failed_calls, sum_latency_ms,
            prompt_tokens, completion_tokens
        ) VALUES
          (1, :old, 'groq', 'm', 'auto', 5, 5, 0, 500, 100, 100),
          (1, :recent, 'groq', 'm', 'auto', 5, 5, 0, 500, 100, 100)
    """).bindparams(old=old_day, recent=recent_day))
    await session.commit()

    purged = await repo.purge_rollups_older_than(730)
    await session.commit()
    assert purged == 1

    remaining = await session.execute(
        text("SELECT COUNT(*) FROM usage_daily_rollup")
    )
    assert remaining.scalar_one() == 1

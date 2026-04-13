"""Usage event repository — writes one row per dispatch, reads analytics."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import UsageEventRow


@dataclass
class UsageEvent:
    provider: str
    model: Optional[str]
    strategy: str
    outcome: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    fallback_position: int = 1
    client_hash: Optional[str] = None
    user_id: Optional[int] = None
    ttfb_ms: Optional[int] = None


@dataclass
class AnalyticsSummary:
    window_seconds: int
    total_calls: int
    success_calls: int
    failed_calls: int
    success_rate: float
    p50_latency_ms: Optional[int]
    p95_latency_ms: Optional[int]
    total_tokens: int
    by_provider: list[dict]   # [{provider, calls, success, avg_latency_ms, tokens}]
    by_strategy: list[dict]   # [{strategy, calls}]
    by_outcome: list[dict]    # [{outcome, calls}]
    by_client: list[dict]     # [{client, calls, success, tokens}]
    time_buckets: list[dict]  # [{bucket_start, calls, success}]


class UsageRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(self, event: UsageEvent) -> None:
        self.session.add(
            UsageEventRow(
                occurred_at=time.time(),
                provider_name=event.provider,
                model=event.model,
                strategy=event.strategy,
                outcome=event.outcome,
                latency_ms=event.latency_ms,
                prompt_tokens=event.prompt_tokens,
                completion_tokens=event.completion_tokens,
                fallback_position=event.fallback_position,
                client_hash=event.client_hash,
                user_id=event.user_id,
                ttfb_ms=event.ttfb_ms,
            )
        )
        # We don't flush/commit here — the caller's session commit batches this
        # alongside the rate_repo.commit() for the same request.

    async def summary(
        self,
        window_seconds: int = 24 * 3600,
        bucket_count: int = 24,
        user_id: Optional[int] = None,
    ) -> AnalyticsSummary:
        """Compute a single analytics snapshot covering the last N seconds.

        One method, several SQL queries — cheaper and clearer than a giant union.
        When user_id is given, only that user's events are included.
        """
        now = time.time()
        since = now - window_seconds

        user_filter = "AND user_id = :uid" if user_id is not None else ""
        params = {"since": since}
        if user_id is not None:
            params["uid"] = user_id

        # Totals + latency percentiles in one shot using percentile_cont.
        totals = await self.session.execute(
            text(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE outcome = 'success') AS success,
                    COALESCE(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY latency_ms), 0) AS p50,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0) AS p95,
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM usage_events
                WHERE occurred_at >= :since {user_filter}
            """).bindparams(**params)
        )
        row = totals.one()
        total, success, p50, p95, tokens = row
        failed = total - success
        rate = (success / total) if total else 0.0

        # Per-provider
        by_provider_result = await self.session.execute(
            text(f"""
                SELECT provider_name,
                       COUNT(*) AS calls,
                       COUNT(*) FILTER (WHERE outcome = 'success') AS success,
                       COALESCE(AVG(latency_ms), 0)::int AS avg_latency,
                       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM usage_events
                WHERE occurred_at >= :since {user_filter}
                GROUP BY provider_name
                ORDER BY calls DESC
            """).bindparams(**params)
        )
        by_provider = [
            {
                "provider": r[0],
                "calls": int(r[1]),
                "success": int(r[2]),
                "avg_latency_ms": int(r[3]),
                "tokens": int(r[4]),
            }
            for r in by_provider_result.all()
        ]

        by_strategy_result = await self.session.execute(
            text(f"""
                SELECT strategy, COUNT(*) AS calls
                FROM usage_events WHERE occurred_at >= :since {user_filter}
                GROUP BY strategy ORDER BY calls DESC
            """).bindparams(**params)
        )
        by_strategy = [{"strategy": r[0], "calls": int(r[1])} for r in by_strategy_result.all()]

        by_outcome_result = await self.session.execute(
            text(f"""
                SELECT outcome, COUNT(*) AS calls
                FROM usage_events WHERE occurred_at >= :since {user_filter}
                GROUP BY outcome ORDER BY calls DESC
            """).bindparams(**params)
        )
        by_outcome = [{"outcome": r[0], "calls": int(r[1])} for r in by_outcome_result.all()]

        # Per-client (join clients table to get human name)
        ue_user_filter = "AND ue.user_id = :uid" if user_id is not None else ""
        by_client_result = await self.session.execute(
            text(f"""
                SELECT COALESCE(c.name, 'unknown') AS client_name,
                       COUNT(*) AS calls,
                       COUNT(*) FILTER (WHERE ue.outcome = 'success') AS success,
                       COALESCE(SUM(ue.prompt_tokens + ue.completion_tokens), 0) AS tokens
                FROM usage_events ue
                LEFT JOIN clients c ON ue.client_hash = c.key_hash
                WHERE ue.occurred_at >= :since {ue_user_filter}
                GROUP BY c.name
                ORDER BY calls DESC
            """).bindparams(**params)
        )
        by_client = [
            {
                "client": r[0],
                "calls": int(r[1]),
                "success": int(r[2]),
                "tokens": int(r[3]),
            }
            for r in by_client_result.all()
        ]

        # Time bucket series — divide the window into N equal buckets and count.
        bucket_width = window_seconds / bucket_count
        bucket_params = {**params, "width": bucket_width}
        buckets_result = await self.session.execute(
            text(f"""
                SELECT
                    FLOOR((occurred_at - :since) / :width)::int AS bucket,
                    COUNT(*) AS calls,
                    COUNT(*) FILTER (WHERE outcome = 'success') AS success
                FROM usage_events
                WHERE occurred_at >= :since {user_filter}
                GROUP BY bucket
                ORDER BY bucket
            """).bindparams(**bucket_params)
        )
        raw_buckets = {int(r[0]): (int(r[1]), int(r[2])) for r in buckets_result.all()}
        # Fill empty buckets with zeros so the frontend gets a dense series
        time_buckets = []
        for i in range(bucket_count):
            calls, succ = raw_buckets.get(i, (0, 0))
            time_buckets.append(
                {
                    "bucket_start": since + i * bucket_width,
                    "calls": calls,
                    "success": succ,
                }
            )

        return AnalyticsSummary(
            window_seconds=window_seconds,
            total_calls=int(total),
            success_calls=int(success),
            failed_calls=int(failed),
            success_rate=round(rate, 4),
            p50_latency_ms=int(p50) if p50 else None,
            p95_latency_ms=int(p95) if p95 else None,
            total_tokens=int(tokens),
            by_provider=by_provider,
            by_strategy=by_strategy,
            by_outcome=by_outcome,
            by_client=by_client,
            time_buckets=time_buckets,
        )

    async def tokens_today_by_provider(self, provider_names: list[str]) -> dict[str, int]:
        """Return {provider_name: total_tokens_last_24h} for the given providers."""
        if not provider_names:
            return {}
        since = time.time() - 86400
        result = await self.session.execute(
            text("""
                SELECT provider_name,
                       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM usage_events
                WHERE provider_name = ANY(:names) AND occurred_at >= :since
                GROUP BY provider_name
            """).bindparams(
                bindparam("names", value=provider_names),
                since=since,
            )
        )
        return {r[0]: int(r[1]) for r in result.all()}

    async def purge_older_than(self, seconds: int) -> int:
        cutoff = time.time() - seconds
        result = await self.session.execute(
            text("DELETE FROM usage_events WHERE occurred_at < :c").bindparams(c=cutoff)
        )
        return result.rowcount

"""Usage event repository — writes one row per dispatch, reads analytics."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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
    # ── Enriched fields (Sprint 7 analytics expansion) ─────────────────
    p99_latency_ms: Optional[int] = None
    avg_ttfb_ms: Optional[int] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors_by_kind: list[dict] = field(default_factory=list)   # [{kind, calls}]
    by_model: list[dict] = field(default_factory=list)         # [{model, calls, success, avg_latency_ms, tokens}]
    fallback_hist: list[dict] = field(default_factory=list)    # [{position, calls}]
    hourly_pattern: list[dict] = field(default_factory=list)   # [{hour, calls}] — 0..23, last 7d


@dataclass
class HistoricalSummary:
    """Long-window (>30d) aggregate read from usage_daily_rollup."""
    days: int
    total_calls: int
    success_calls: int
    failed_calls: int
    success_rate: float
    total_tokens: int
    daily: list[dict]        # [{day, calls, success, tokens, p95_latency_ms}]
    by_provider: list[dict]  # [{provider, calls, success, tokens, avg_latency_ms}]
    by_model: list[dict]     # [{model, calls, success, tokens, avg_latency_ms}]


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

        # Totals + latency percentiles (P50/P95/P99) + TTFB + token split in one shot.
        totals = await self.session.execute(
            text(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE outcome = 'success') AS success,
                    COALESCE(PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY latency_ms), 0) AS p50,
                    COALESCE(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms), 0) AS p95,
                    COALESCE(PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms), 0) AS p99,
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_sum,
                    COALESCE(SUM(completion_tokens), 0) AS completion_sum,
                    AVG(ttfb_ms) FILTER (WHERE ttfb_ms IS NOT NULL) AS avg_ttfb
                FROM usage_events
                WHERE occurred_at >= :since {user_filter}
            """).bindparams(**params)
        )
        row = totals.one()
        total, success, p50, p95, p99, tokens, prompt_sum, completion_sum, avg_ttfb = row
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

        # Errors by kind — same shape as outcome but filters out 'success'.
        errors_by_kind = [
            {"kind": r["outcome"], "calls": r["calls"]}
            for r in by_outcome if r["outcome"] != "success"
        ]

        # Per-model — top 10 by calls, group NULL models as '(unknown)'.
        by_model_result = await self.session.execute(
            text(f"""
                SELECT COALESCE(model, '(unknown)') AS model_name,
                       COUNT(*) AS calls,
                       COUNT(*) FILTER (WHERE outcome = 'success') AS success,
                       COALESCE(AVG(latency_ms), 0)::int AS avg_latency,
                       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM usage_events
                WHERE occurred_at >= :since {user_filter}
                GROUP BY model_name
                ORDER BY calls DESC
                LIMIT 10
            """).bindparams(**params)
        )
        by_model = [
            {
                "model": r[0],
                "calls": int(r[1]),
                "success": int(r[2]),
                "avg_latency_ms": int(r[3]),
                "tokens": int(r[4]),
            }
            for r in by_model_result.all()
        ]

        # Fallback chain histogram — clamps position >=3 into a single bucket.
        fallback_result = await self.session.execute(
            text(f"""
                SELECT LEAST(fallback_position, 3) AS pos,
                       COUNT(*) AS calls
                FROM usage_events
                WHERE occurred_at >= :since {user_filter}
                GROUP BY pos
                ORDER BY pos
            """).bindparams(**params)
        )
        fallback_hist = [
            {"position": int(r[0]), "calls": int(r[1])}
            for r in fallback_result.all()
        ]

        # Hourly pattern — always uses last 7 days regardless of window
        # (a weekly pattern needs a week of data to be meaningful).
        hourly_since = now - 7 * 86400
        hourly_params = {"since": hourly_since}
        if user_id is not None:
            hourly_params["uid"] = user_id
        hourly_result = await self.session.execute(
            text(f"""
                SELECT EXTRACT(HOUR FROM to_timestamp(occurred_at))::int AS hour,
                       COUNT(*) AS calls
                FROM usage_events
                WHERE occurred_at >= :since {user_filter}
                GROUP BY hour
                ORDER BY hour
            """).bindparams(**hourly_params)
        )
        hourly_map = {int(r[0]): int(r[1]) for r in hourly_result.all()}
        hourly_pattern = [
            {"hour": h, "calls": hourly_map.get(h, 0)}
            for h in range(24)
        ]

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
            p99_latency_ms=int(p99) if p99 else None,
            avg_ttfb_ms=int(avg_ttfb) if avg_ttfb is not None else None,
            total_tokens=int(tokens),
            prompt_tokens=int(prompt_sum),
            completion_tokens=int(completion_sum),
            by_provider=by_provider,
            by_strategy=by_strategy,
            by_outcome=by_outcome,
            errors_by_kind=errors_by_kind,
            by_model=by_model,
            fallback_hist=fallback_hist,
            hourly_pattern=hourly_pattern,
            by_client=by_client,
            time_buckets=time_buckets,
        )

    async def rollup_day(self, day: date) -> int:
        """Upsert the rollup row(s) for a single UTC day from raw usage_events.

        Groups by (user_id, provider, model, strategy) and stores percentiles
        pre-computed. Called hourly by the background task in main.py — once
        for today (still accumulating) and once for yesterday (final close-out
        that catches any late-arrival rows). Returns number of rows upserted.
        """
        start_dt = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=1)
        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()

        # Delete existing rollup rows for this day before re-inserting — simplest
        # way to handle corrections/late arrivals without parsing ON CONFLICT
        # for every aggregate column.
        await self.session.execute(
            text("DELETE FROM usage_daily_rollup WHERE day = :d").bindparams(d=day)
        )

        result = await self.session.execute(
            text("""
                INSERT INTO usage_daily_rollup (
                    user_id, day, provider_name, model, strategy,
                    total_calls, success_calls, failed_calls,
                    sum_latency_ms,
                    p50_latency_ms, p95_latency_ms, p99_latency_ms,
                    avg_ttfb_ms,
                    prompt_tokens, completion_tokens,
                    errors_by_kind, fallback_position_hist,
                    updated_at
                )
                SELECT
                    COALESCE(user_id, 0) AS user_id,
                    :day AS day,
                    provider_name,
                    COALESCE(model, '') AS model,
                    strategy,
                    COUNT(*) AS total_calls,
                    COUNT(*) FILTER (WHERE outcome = 'success') AS success_calls,
                    COUNT(*) FILTER (WHERE outcome <> 'success') AS failed_calls,
                    COALESCE(SUM(latency_ms), 0) AS sum_latency_ms,
                    PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY latency_ms)::int AS p50,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95,
                    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms)::int AS p99,
                    AVG(ttfb_ms) FILTER (WHERE ttfb_ms IS NOT NULL)::int AS avg_ttfb,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(
                        jsonb_object_agg(outcome, cnt) FILTER (WHERE outcome <> 'success'),
                        '{}'::jsonb
                    ) AS errors_by_kind,
                    COALESCE(
                        jsonb_object_agg(fpos::text, fpos_cnt),
                        '{}'::jsonb
                    ) AS fallback_position_hist,
                    EXTRACT(EPOCH FROM NOW())
                FROM (
                    SELECT
                        user_id, provider_name, model, strategy, outcome,
                        latency_ms, ttfb_ms, prompt_tokens, completion_tokens,
                        LEAST(fallback_position, 3) AS fpos,
                        COUNT(*) OVER (
                            PARTITION BY COALESCE(user_id, 0), provider_name,
                                         COALESCE(model, ''), strategy, outcome
                        ) AS cnt,
                        COUNT(*) OVER (
                            PARTITION BY COALESCE(user_id, 0), provider_name,
                                         COALESCE(model, ''), strategy,
                                         LEAST(fallback_position, 3)
                        ) AS fpos_cnt
                    FROM usage_events
                    WHERE occurred_at >= :start_ts AND occurred_at < :end_ts
                ) src
                GROUP BY user_id, provider_name, model, strategy
            """).bindparams(day=day, start_ts=start_ts, end_ts=end_ts)
        )
        return result.rowcount or 0

    async def historical_summary(
        self, days: int, user_id: Optional[int] = None
    ) -> HistoricalSummary:
        """Long-window aggregate read from usage_daily_rollup.

        Cheap even over 365d because it scans pre-aggregated daily rows
        rather than raw events.
        """
        today = datetime.now(timezone.utc).date()
        start_day = today - timedelta(days=days - 1)

        user_filter = "AND user_id = :uid" if user_id is not None else ""
        params = {"start": start_day}
        if user_id is not None:
            params["uid"] = user_id

        totals_result = await self.session.execute(
            text(f"""
                SELECT
                    COALESCE(SUM(total_calls), 0) AS total,
                    COALESCE(SUM(success_calls), 0) AS success,
                    COALESCE(SUM(failed_calls), 0) AS failed,
                    COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM usage_daily_rollup
                WHERE day >= :start {user_filter}
            """).bindparams(**params)
        )
        total, success, failed, tokens = totals_result.one()
        rate = (success / total) if total else 0.0

        # One row per day — P95 is the *max* of per-bucket P95s, which is a
        # reasonable approximation without storing raw latencies.
        daily_result = await self.session.execute(
            text(f"""
                SELECT day,
                       SUM(total_calls) AS calls,
                       SUM(success_calls) AS success,
                       SUM(prompt_tokens + completion_tokens) AS tokens,
                       MAX(p95_latency_ms) AS p95
                FROM usage_daily_rollup
                WHERE day >= :start {user_filter}
                GROUP BY day
                ORDER BY day
            """).bindparams(**params)
        )
        daily_rows = {r[0]: r for r in daily_result.all()}

        # Dense series: fill missing days with zeros so the chart is continuous.
        daily = []
        for i in range(days):
            d = start_day + timedelta(days=i)
            if d in daily_rows:
                r = daily_rows[d]
                daily.append({
                    "day": d.isoformat(),
                    "calls": int(r[1] or 0),
                    "success": int(r[2] or 0),
                    "tokens": int(r[3] or 0),
                    "p95_latency_ms": int(r[4]) if r[4] else None,
                })
            else:
                daily.append({
                    "day": d.isoformat(),
                    "calls": 0,
                    "success": 0,
                    "tokens": 0,
                    "p95_latency_ms": None,
                })

        by_provider_result = await self.session.execute(
            text(f"""
                SELECT provider_name,
                       SUM(total_calls) AS calls,
                       SUM(success_calls) AS success,
                       SUM(prompt_tokens + completion_tokens) AS tokens,
                       CASE WHEN SUM(total_calls) > 0
                            THEN (SUM(sum_latency_ms) / SUM(total_calls))::int
                            ELSE 0 END AS avg_latency
                FROM usage_daily_rollup
                WHERE day >= :start {user_filter}
                GROUP BY provider_name
                ORDER BY calls DESC
            """).bindparams(**params)
        )
        by_provider = [
            {
                "provider": r[0],
                "calls": int(r[1] or 0),
                "success": int(r[2] or 0),
                "tokens": int(r[3] or 0),
                "avg_latency_ms": int(r[4] or 0),
            }
            for r in by_provider_result.all()
        ]

        by_model_result = await self.session.execute(
            text(f"""
                SELECT CASE WHEN model = '' THEN '(unknown)' ELSE model END AS model_name,
                       SUM(total_calls) AS calls,
                       SUM(success_calls) AS success,
                       SUM(prompt_tokens + completion_tokens) AS tokens,
                       CASE WHEN SUM(total_calls) > 0
                            THEN (SUM(sum_latency_ms) / SUM(total_calls))::int
                            ELSE 0 END AS avg_latency
                FROM usage_daily_rollup
                WHERE day >= :start {user_filter}
                GROUP BY model_name
                ORDER BY calls DESC
                LIMIT 10
            """).bindparams(**params)
        )
        by_model = [
            {
                "model": r[0],
                "calls": int(r[1] or 0),
                "success": int(r[2] or 0),
                "tokens": int(r[3] or 0),
                "avg_latency_ms": int(r[4] or 0),
            }
            for r in by_model_result.all()
        ]

        return HistoricalSummary(
            days=days,
            total_calls=int(total),
            success_calls=int(success),
            failed_calls=int(failed),
            success_rate=round(rate, 4),
            total_tokens=int(tokens),
            daily=daily,
            by_provider=by_provider,
            by_model=by_model,
        )

    async def purge_rollups_older_than(self, days: int) -> int:
        """Drop rollup rows older than `days` days (default caller: 730)."""
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        result = await self.session.execute(
            text("DELETE FROM usage_daily_rollup WHERE day < :c").bindparams(c=cutoff)
        )
        return result.rowcount or 0

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

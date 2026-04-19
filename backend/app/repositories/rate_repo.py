"""Rate repository — atomic reservation backed by Postgres.

Uses the `freeai_try_reserve(user_id, name, rpm, rpd)` plpgsql function.
All operations are scoped to a specific user_id for multi-user isolation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import bindparam, case, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ProviderStatsRow, RateEventRow
from ..metrics import provider_circuit_breaker_trips_total

# Errors that should NOT count toward the unhealthy streak.
# Outcomes that don't signal provider ill-health — provider is alive and
# reachable, it just refused this specific request. These must not trip the
# consecutive_failures streak that quarantines a provider.
_BENIGN_ERRORS = {"rate_limited", "client_error", "content_filtered"}


@dataclass
class ReservationToken:
    event_id: int
    user_id: int
    provider: str
    timestamp: float


@dataclass
class ProviderSnapshot:
    requests_today: int
    requests_this_minute: int
    last_error: Optional[str]
    last_error_kind: Optional[str]
    last_latency_ms: Optional[int]
    latency_ema_ms: Optional[float]
    healthy: bool
    quarantined_until: Optional[float]
    total_calls: int
    total_failures: int
    tokens_today: int


class RateRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ──────────────── reservation ────────────────

    async def try_reserve(
        self,
        user_id: int,
        provider: str,
        rpm_limit: Optional[int],
        rpd_limit: Optional[int],
    ) -> Optional[ReservationToken]:
        """Atomically check capacity and insert a rate event. Returns None if
        the provider is at capacity / quarantined."""
        result = await self.session.execute(
            text("SELECT freeai_try_reserve(:uid, :p, :rpm, :rpd)").bindparams(
                bindparam("uid", value=user_id),
                bindparam("p", value=provider),
                bindparam("rpm", value=rpm_limit),
                bindparam("rpd", value=rpd_limit),
            )
        )
        event_id = result.scalar_one_or_none()
        if event_id is None:
            return None
        return ReservationToken(
            event_id=event_id, user_id=user_id,
            provider=provider, timestamp=time.time(),
        )

    async def commit(
        self,
        reservation: ReservationToken,
        latency_ms: int,
        ok: bool,
        error: Optional[str] = None,
        error_kind: Optional[str] = None,
        quarantine_seconds: Optional[float] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        circuit_breaker_threshold: int = 3,
        circuit_breaker_window_s: int = 300,
        circuit_breaker_base_cooldown_s: int = 30,
        circuit_breaker_max_cooldown_s: int = 3600,
    ) -> None:
        uid = reservation.user_id
        pname = reservation.provider
        pk_filter = (
            (ProviderStatsRow.user_id == uid)
            & (ProviderStatsRow.provider_name == pname)
        )

        if ok:
            _EMA_ALPHA = 0.3
            now = time.time()
            tokens_added = prompt_tokens + completion_tokens

            ema_col = ProviderStatsRow.latency_ema_ms
            new_ema = case(
                (ema_col.is_(None), float(latency_ms)),
                else_=_EMA_ALPHA * latency_ms + (1 - _EMA_ALPHA) * ema_col,
            )

            day_col = ProviderStatsRow.tokens_day_start
            tok_col = ProviderStatsRow.tokens_today
            day_rolled = (now - day_col) >= 86400
            new_tokens = case(
                (day_rolled, tokens_added),
                else_=tok_col + tokens_added,
            )
            new_day_start = case(
                (day_rolled, now),
                else_=day_col,
            )

            await self.session.execute(
                update(ProviderStatsRow)
                .where(pk_filter)
                .values(
                    last_latency_ms=latency_ms,
                    latency_ema_ms=new_ema,
                    last_error=None,
                    last_error_kind=None,
                    consecutive_failures=0,
                    recent_failures_started_at=0.0,
                    cooldown_level=0,
                    healthy=True,
                    quarantined_until=0.0,
                    total_calls=ProviderStatsRow.total_calls + 1,
                    tokens_today=new_tokens,
                    tokens_day_start=new_day_start,
                )
            )
            return

        # failure path
        if error_kind in _BENIGN_ERRORS:
            new_quarantine = None
            if quarantine_seconds:
                new_quarantine = time.time() + quarantine_seconds
            values = {
                "last_latency_ms": latency_ms,
                "last_error": error,
                "last_error_kind": error_kind,
                "total_calls": ProviderStatsRow.total_calls + 1,
                "total_failures": ProviderStatsRow.total_failures + 1,
            }
            if new_quarantine is not None:
                values["quarantined_until"] = func_greatest(
                    ProviderStatsRow.quarantined_until, new_quarantine
                )
            await self.session.execute(
                update(ProviderStatsRow).where(pk_filter).values(**values)
            )
            return

        # non-benign failure — feeds the circuit breaker.
        # Sliding window: if the last failure is older than the window, reset
        # the streak before counting this one. Exponential cooldown_level
        # escalates each time the breaker trips; resets on success.
        result = await self.session.execute(
            select(ProviderStatsRow).where(pk_filter)
        )
        row = result.scalar_one_or_none()
        now = time.time()
        window_start = (row.recent_failures_started_at if row else 0.0) or 0.0
        prior_streak = row.consecutive_failures if row else 0
        within_window = (
            prior_streak > 0
            and window_start > 0
            and (now - window_start) <= circuit_breaker_window_s
        )
        new_streak = (prior_streak + 1) if within_window else 1
        window_started = window_start if within_window else now
        values = {
            "last_latency_ms": latency_ms,
            "last_error": error,
            "last_error_kind": error_kind,
            "consecutive_failures": new_streak,
            "recent_failures_started_at": window_started,
            "total_calls": ProviderStatsRow.total_calls + 1,
            "total_failures": ProviderStatsRow.total_failures + 1,
        }
        threshold = max(1, circuit_breaker_threshold)
        tripped = new_streak >= threshold
        if tripped:
            values["healthy"] = False
            prior_level = row.cooldown_level if row else 0
            # Cap the exponent so 2**level can't overflow absurdly.
            level = min(prior_level, 20)
            cooldown = min(
                circuit_breaker_max_cooldown_s,
                circuit_breaker_base_cooldown_s * (2 ** level),
            )
            values["quarantined_until"] = now + cooldown
            values["cooldown_level"] = prior_level + 1
            # Start a fresh window after tripping so we don't re-trip
            # instantly on the very next failure.
            values["consecutive_failures"] = 0
            values["recent_failures_started_at"] = 0.0

        await self.session.execute(
            update(ProviderStatsRow).where(pk_filter).values(**values)
        )
        if tripped:
            provider_circuit_breaker_trips_total.labels(provider=pname).inc()

    async def rollback(self, reservation: ReservationToken) -> None:
        await self.session.execute(
            delete(RateEventRow).where(RateEventRow.id == reservation.event_id)
        )

    # ──────────────── reads ────────────────

    async def snapshot(self, user_id: int, provider: str) -> ProviderSnapshot:
        now = time.time()
        result = await self.session.execute(
            text("""
                SELECT
                  COUNT(*) FILTER (WHERE occurred_at >= :now - 60)    AS rpm,
                  COUNT(*) FILTER (WHERE occurred_at >= :now - 86400) AS rpd
                FROM rate_events
                WHERE user_id = :uid AND provider_name = :p
            """).bindparams(now=now, uid=user_id, p=provider)
        )
        rpm, rpd = result.one()

        stats_result = await self.session.execute(
            select(ProviderStatsRow).where(
                ProviderStatsRow.user_id == user_id,
                ProviderStatsRow.provider_name == provider,
            )
        )
        stats = stats_result.scalar_one_or_none()
        if not stats:
            return ProviderSnapshot(
                requests_today=int(rpd or 0),
                requests_this_minute=int(rpm or 0),
                last_error=None, last_error_kind=None,
                last_latency_ms=None, latency_ema_ms=None,
                healthy=True, quarantined_until=None,
                total_calls=0, total_failures=0, tokens_today=0,
            )

        quarantine_active = stats.quarantined_until > now
        effective_healthy = not quarantine_active and (stats.healthy or stats.quarantined_until > 0)
        quarantine_display = stats.quarantined_until if quarantine_active else None
        tokens_today = stats.tokens_today if (now - stats.tokens_day_start < 86400) else 0

        return ProviderSnapshot(
            requests_today=int(rpd or 0),
            requests_this_minute=int(rpm or 0),
            last_error=stats.last_error, last_error_kind=stats.last_error_kind,
            last_latency_ms=stats.last_latency_ms,
            latency_ema_ms=stats.latency_ema_ms,
            healthy=effective_healthy, quarantined_until=quarantine_display,
            total_calls=stats.total_calls, total_failures=stats.total_failures,
            tokens_today=tokens_today,
        )

    async def snapshot_all(
        self,
        user_id: int,
        providers: list[str],
        counter_store=None,
    ) -> dict[str, ProviderSnapshot]:
        if not providers:
            return {}
        now = time.time()

        if counter_store is not None:
            counts = {name: counter_store.counts(user_id, name) for name in providers}
        else:
            counts_result = await self.session.execute(
                text("""
                    SELECT provider_name,
                           COUNT(*) FILTER (WHERE occurred_at >= :now - 60)    AS rpm,
                           COUNT(*) FILTER (WHERE occurred_at >= :now - 86400) AS rpd
                    FROM rate_events
                    WHERE user_id = :uid AND provider_name = ANY(:names)
                    GROUP BY provider_name
                """).bindparams(
                    bindparam("now", value=now),
                    bindparam("uid", value=user_id),
                    bindparam("names", value=providers),
                )
            )
            counts = {r[0]: (int(r[1]), int(r[2])) for r in counts_result.all()}

        stats_result = await self.session.execute(
            select(ProviderStatsRow).where(
                ProviderStatsRow.user_id == user_id,
                ProviderStatsRow.provider_name.in_(providers),
            )
        )
        stats_map = {s.provider_name: s for s in stats_result.scalars().all()}

        snapshots: dict[str, ProviderSnapshot] = {}
        for name in providers:
            rpm, rpd = counts.get(name, (0, 0))
            stats = stats_map.get(name)
            if not stats:
                snapshots[name] = ProviderSnapshot(
                    requests_today=rpd, requests_this_minute=rpm,
                    last_error=None, last_error_kind=None, last_latency_ms=None,
                    latency_ema_ms=None,
                    healthy=True, quarantined_until=None,
                    total_calls=0, total_failures=0, tokens_today=0,
                )
                continue
            quarantine_active = stats.quarantined_until > now
            effective_healthy = not quarantine_active and (stats.healthy or stats.quarantined_until > 0)
            quarantine_display = stats.quarantined_until if quarantine_active else None
            tokens_today = stats.tokens_today if (now - stats.tokens_day_start < 86400) else 0
            snapshots[name] = ProviderSnapshot(
                requests_today=rpd, requests_this_minute=rpm,
                last_error=stats.last_error, last_error_kind=stats.last_error_kind,
                last_latency_ms=stats.last_latency_ms,
                latency_ema_ms=stats.latency_ema_ms,
                healthy=effective_healthy, quarantined_until=quarantine_display,
                total_calls=stats.total_calls, total_failures=stats.total_failures,
                tokens_today=tokens_today,
            )
        return snapshots

    async def reset_health(self, user_id: int, provider: str) -> None:
        await self.session.execute(
            update(ProviderStatsRow)
            .where(
                ProviderStatsRow.user_id == user_id,
                ProviderStatsRow.provider_name == provider,
            )
            .values(
                healthy=True,
                consecutive_failures=0,
                last_error=None,
                last_error_kind=None,
                quarantined_until=0.0,
            )
        )

    async def purge_old_events(self, older_than_seconds: float = 86400 * 2) -> int:
        cutoff = time.time() - older_than_seconds
        result = await self.session.execute(
            delete(RateEventRow).where(RateEventRow.occurred_at < cutoff)
        )
        return result.rowcount


def func_greatest(a, b):
    from sqlalchemy import func
    return func.greatest(a, b)

"""Rate repository — atomic reservation backed by Postgres.

Uses the `freeai_try_reserve(name, rpm, rpd)` plpgsql function created in
migration 0001. The function does the count + insert in one statement under
row locks, so multiple pods reserving against the same provider see consistent
counters.

Reservation is committed (no-op for the counters — they were already incremented
at reserve time) or rolled back (deletes the rate_event row) by the orchestrator
once the HTTP call comes back.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import bindparam, delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ProviderStatsRow, RateEventRow

# Errors that should NOT count toward the unhealthy streak.
_BENIGN_ERRORS = {"rate_limited", "client_error"}


@dataclass
class ReservationToken:
    event_id: int
    provider: str
    timestamp: float


@dataclass
class ProviderSnapshot:
    requests_today: int
    requests_this_minute: int
    last_error: Optional[str]
    last_error_kind: Optional[str]
    last_latency_ms: Optional[int]
    healthy: bool
    quarantined_until: Optional[float]
    total_calls: int
    total_failures: int


class RateRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # ──────────────── reservation ────────────────

    async def try_reserve(
        self,
        provider: str,
        rpm_limit: Optional[int],
        rpd_limit: Optional[int],
    ) -> Optional[ReservationToken]:
        """Atomically check capacity and insert a rate event. Returns None if
        the provider is at capacity / quarantined."""
        result = await self.session.execute(
            text("SELECT freeai_try_reserve(:p, :rpm, :rpd)").bindparams(
                bindparam("p", value=provider),
                bindparam("rpm", value=rpm_limit),
                bindparam("rpd", value=rpd_limit),
            )
        )
        event_id = result.scalar_one_or_none()
        if event_id is None:
            return None
        return ReservationToken(event_id=event_id, provider=provider, timestamp=time.time())

    async def commit(
        self,
        reservation: ReservationToken,
        latency_ms: int,
        ok: bool,
        error: Optional[str] = None,
        error_kind: Optional[str] = None,
        quarantine_seconds: Optional[float] = None,
    ) -> None:
        # Update stats row (always exists — try_reserve created it)
        if ok:
            # A successful call fully heals the provider: clear the streak,
            # mark healthy, AND clear any lingering quarantine timestamp so
            # snapshot() doesn't have to reason about "expired but not cleared".
            # Before this fix, quarantined_until kept its old value after a
            # successful call and read paths had to carry the expiration logic
            # (which had its own bug). See REVIEW § 1.2.
            await self.session.execute(
                update(ProviderStatsRow)
                .where(ProviderStatsRow.provider_name == reservation.provider)
                .values(
                    last_latency_ms=latency_ms,
                    last_error=None,
                    last_error_kind=None,
                    consecutive_failures=0,
                    healthy=True,
                    quarantined_until=0.0,
                    total_calls=ProviderStatsRow.total_calls + 1,
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
                # don't lower an existing longer quarantine
                values["quarantined_until"] = func_greatest(
                    ProviderStatsRow.quarantined_until, new_quarantine
                )
            await self.session.execute(
                update(ProviderStatsRow)
                .where(ProviderStatsRow.provider_name == reservation.provider)
                .values(**values)
            )
            return

        # non-benign failure — increment streak, possibly quarantine
        # Pull current streak/quarantine to decide backoff
        row = await self.session.get(ProviderStatsRow, reservation.provider)
        new_streak = (row.consecutive_failures if row else 0) + 1
        values = {
            "last_latency_ms": latency_ms,
            "last_error": error,
            "last_error_kind": error_kind,
            "consecutive_failures": new_streak,
            "total_calls": ProviderStatsRow.total_calls + 1,
            "total_failures": ProviderStatsRow.total_failures + 1,
        }
        if new_streak >= 3:
            values["healthy"] = False
            current_quarantine = (row.quarantined_until if row else 0) or 0
            now = time.time()
            if current_quarantine <= now:
                values["quarantined_until"] = now + 30
            else:
                # double the existing window, capped at 10 min
                extra = min(600, max(60, (current_quarantine - now) * 2))
                values["quarantined_until"] = now + extra

        await self.session.execute(
            update(ProviderStatsRow)
            .where(ProviderStatsRow.provider_name == reservation.provider)
            .values(**values)
        )

    async def rollback(self, reservation: ReservationToken) -> None:
        """Release a reservation we never used. Deletes the rate_event row."""
        await self.session.execute(
            delete(RateEventRow).where(RateEventRow.id == reservation.event_id)
        )

    # ──────────────── reads ────────────────

    async def snapshot(self, provider: str) -> ProviderSnapshot:
        """Read-only view of a provider's current state.

        `healthy` in the returned snapshot means "usable for the next request".
        We derive it from two inputs:
          - the stored `healthy` flag (flipped to false after a streak of 3+
            non-benign failures)
          - the `quarantined_until` timestamp (epoch)

        A provider becomes usable again when its quarantine window has elapsed.
        The previous version tried to compute this but ANDed back against the
        stale `stats.healthy` at the end, keeping quarantined providers dead
        forever. See REVIEW § 1.2 for the incident report.
        """
        now = time.time()
        result = await self.session.execute(
            text("""
                SELECT
                  COUNT(*) FILTER (WHERE occurred_at >= :now - 60)    AS rpm,
                  COUNT(*) FILTER (WHERE occurred_at >= :now - 86400) AS rpd
                FROM rate_events WHERE provider_name = :p
            """).bindparams(now=now, p=provider)
        )
        rpm, rpd = result.one()

        stats = await self.session.get(ProviderStatsRow, provider)
        if not stats:
            return ProviderSnapshot(
                requests_today=int(rpd or 0),
                requests_this_minute=int(rpm or 0),
                last_error=None,
                last_error_kind=None,
                last_latency_ms=None,
                healthy=True,
                quarantined_until=None,
                total_calls=0,
                total_failures=0,
            )

        # Effective health: if we're inside an active quarantine window, we're
        # unusable regardless of what `healthy` says. Otherwise, quarantine is
        # over (or never was) and we trust `healthy`. If quarantine expired we
        # also treat `healthy` as true — the snapshot shouldn't be the reason a
        # provider stays dead after its window.
        quarantine_active = stats.quarantined_until > now
        effective_healthy = not quarantine_active and (stats.healthy or stats.quarantined_until > 0)
        # quarantine field returned to callers: None once expired
        quarantine_display = stats.quarantined_until if quarantine_active else None

        return ProviderSnapshot(
            requests_today=int(rpd or 0),
            requests_this_minute=int(rpm or 0),
            last_error=stats.last_error,
            last_error_kind=stats.last_error_kind,
            last_latency_ms=stats.last_latency_ms,
            healthy=effective_healthy,
            quarantined_until=quarantine_display,
            total_calls=stats.total_calls,
            total_failures=stats.total_failures,
        )

    async def snapshot_all(self, providers: list[str]) -> dict[str, ProviderSnapshot]:
        """Batched version of snapshot() — 2 queries instead of 2×N."""
        if not providers:
            return {}
        now = time.time()
        counts_result = await self.session.execute(
            text("""
                SELECT provider_name,
                       COUNT(*) FILTER (WHERE occurred_at >= :now - 60)    AS rpm,
                       COUNT(*) FILTER (WHERE occurred_at >= :now - 86400) AS rpd
                FROM rate_events
                WHERE provider_name = ANY(:names)
                GROUP BY provider_name
            """).bindparams(
                bindparam("now", value=now),
                bindparam("names", value=providers),
            )
        )
        counts = {r[0]: (int(r[1]), int(r[2])) for r in counts_result.all()}

        stats_result = await self.session.execute(
            select(ProviderStatsRow).where(ProviderStatsRow.provider_name.in_(providers))
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
                    healthy=True, quarantined_until=None,
                    total_calls=0, total_failures=0,
                )
                continue
            quarantine_active = stats.quarantined_until > now
            effective_healthy = not quarantine_active and (stats.healthy or stats.quarantined_until > 0)
            quarantine_display = stats.quarantined_until if quarantine_active else None
            snapshots[name] = ProviderSnapshot(
                requests_today=rpd, requests_this_minute=rpm,
                last_error=stats.last_error, last_error_kind=stats.last_error_kind,
                last_latency_ms=stats.last_latency_ms,
                healthy=effective_healthy, quarantined_until=quarantine_display,
                total_calls=stats.total_calls, total_failures=stats.total_failures,
            )
        return snapshots

    async def reset_health(self, provider: str) -> None:
        await self.session.execute(
            update(ProviderStatsRow)
            .where(ProviderStatsRow.provider_name == provider)
            .values(
                healthy=True,
                consecutive_failures=0,
                last_error=None,
                last_error_kind=None,
                quarantined_until=0.0,
            )
        )

    async def purge_old_events(self, older_than_seconds: float = 86400 * 2) -> int:
        """Bound the rate_events table. Call from a periodic task."""
        cutoff = time.time() - older_than_seconds
        result = await self.session.execute(
            delete(RateEventRow).where(RateEventRow.occurred_at < cutoff)
        )
        return result.rowcount


def func_greatest(a, b):
    """SQLAlchemy helper for postgres GREATEST(...)."""
    from sqlalchemy import func
    return func.greatest(a, b)

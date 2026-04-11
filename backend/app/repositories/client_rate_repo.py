"""Per-client rate limiting repository.

Thin wrapper over the `freeai_try_reserve_client` plpgsql function. Separate
from RateRepository (which is for *providers*) so the two concerns don't
share a table or a function signature — reusing rate_events with synthetic
'client:xxx' names was the source of REVIEW § 1.1.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import bindparam, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ClientRateEventRow


@dataclass
class ClientReservation:
    event_id: int
    client_hash: str
    timestamp: float


class ClientRateRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def try_acquire(self, client_hash: str, rpm_limit: int) -> bool:
        """Returns True if the request is allowed, False if the client is
        over its per-minute cap. Atomic under concurrency because the plpgsql
        function takes an xact advisory lock keyed on the client hash."""
        result = await self.session.execute(
            text("SELECT freeai_try_reserve_client(:h, :rpm)").bindparams(
                bindparam("h", value=client_hash),
                bindparam("rpm", value=rpm_limit),
            )
        )
        return result.scalar_one_or_none() is not None

    async def purge_older_than(self, seconds: float) -> int:
        """Delete events older than `seconds` ago. Intended to be called from
        a scheduled job — for now, unused (REVIEW § 3)."""
        cutoff = time.time() - seconds
        result = await self.session.execute(
            delete(ClientRateEventRow).where(ClientRateEventRow.occurred_at < cutoff)
        )
        return result.rowcount

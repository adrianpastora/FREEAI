"""In-memory rate counters — eliminates COUNT queries from the hot path.

Maintains per-(user, provider) deques of call timestamps. Used by
``snapshot_all()`` to read rpm/rpd without hitting ``rate_events``.

The plpgsql ``freeai_try_reserve`` function remains the correctness
gate for actual reservation (it runs under row locks in Postgres).
These counters are an optimization for the scoring/ranking path where
slight staleness is acceptable.

Single-pod only: if running multiple pods, each pod tracks its own
counters. The SQL reservation function handles cross-pod consistency.
"""
from __future__ import annotations

import time
from collections import deque


class RateCounterStore:
    """Per-(user, provider) sliding-window counters backed by timestamp deques."""

    def __init__(self):
        self._events: dict[tuple[int, str], deque[float]] = {}

    def record(self, user_id: int, provider: str) -> None:
        key = (user_id, provider)
        dq = self._events.get(key)
        if dq is None:
            dq = deque()
            self._events[key] = dq
        dq.append(time.time())

    def _prune(self, user_id: int, provider: str, now: float) -> deque[float]:
        key = (user_id, provider)
        dq = self._events.get(key)
        if dq is None:
            return deque()
        cutoff = now - 86400
        while dq and dq[0] < cutoff:
            dq.popleft()
        return dq

    def rpm(self, user_id: int, provider: str) -> int:
        now = time.time()
        dq = self._prune(user_id, provider, now)
        cutoff = now - 60
        return sum(1 for t in dq if t >= cutoff)

    def rpd(self, user_id: int, provider: str) -> int:
        now = time.time()
        dq = self._prune(user_id, provider, now)
        return len(dq)

    def counts(self, user_id: int, provider: str) -> tuple[int, int]:
        """Return (rpm, rpd) in a single pass."""
        now = time.time()
        dq = self._prune(user_id, provider, now)
        cutoff_min = now - 60
        rpm = sum(1 for t in dq if t >= cutoff_min)
        return rpm, len(dq)

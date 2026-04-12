"""In-memory rate counters — eliminates COUNT queries from the hot path.

Maintains per-provider deques of call timestamps. Used by
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
    """Per-provider sliding-window counters backed by timestamp deques."""

    def __init__(self):
        self._events: dict[str, deque[float]] = {}

    def record(self, provider: str) -> None:
        """Record a call to ``provider`` at the current time."""
        dq = self._events.get(provider)
        if dq is None:
            dq = deque()
            self._events[provider] = dq
        dq.append(time.time())

    def _prune(self, provider: str, now: float) -> deque[float]:
        """Remove entries older than 24h and return the deque."""
        dq = self._events.get(provider)
        if dq is None:
            return deque()
        cutoff = now - 86400
        while dq and dq[0] < cutoff:
            dq.popleft()
        return dq

    def rpm(self, provider: str) -> int:
        """Requests in the last 60 seconds."""
        now = time.time()
        dq = self._prune(provider, now)
        cutoff = now - 60
        return sum(1 for t in dq if t >= cutoff)

    def rpd(self, provider: str) -> int:
        """Requests in the last 24 hours."""
        now = time.time()
        dq = self._prune(provider, now)
        return len(dq)

    def counts(self, provider: str) -> tuple[int, int]:
        """Return (rpm, rpd) in a single pass."""
        now = time.time()
        dq = self._prune(provider, now)
        cutoff_min = now - 60
        rpm = sum(1 for t in dq if t >= cutoff_min)
        return rpm, len(dq)

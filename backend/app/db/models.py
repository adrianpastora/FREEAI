"""ORM models. Eight tables:

  • app_config          — singleton row (id=1) with default_strategy / fallback flag
  • providers           — one row per registered provider with config + encrypted key
  • provider_stats      — current rolling state per provider (health, quarantine, last error)
  • rate_events         — append-only log of provider calls; used to compute rpm/rpd
  • clients             — issued API keys (hash only) for inbound auth
  • client_rate_events  — append-only log of inbound calls for per-client rate limits
  • usage_events        — telemetry for the analytics dashboard (separate retention)
  • strategies          — routing strategies as data (seeded + user-editable)

The rate-limit check is "count rows in rate_events for provider X with timestamp ≥ now-60s
inserted within the same transaction." That's how we get atomic reservation across pods —
see the freeai_try_reserve plpgsql function in the initial migration.
"""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .engine import Base


class AppConfigRow(Base):
    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    default_strategy: Mapped[str] = mapped_column(String(32), default="auto", nullable=False)
    enable_fallback: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    admin_token_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[float] = mapped_column(
        Float, default=time.time, onupdate=time.time, nullable=False
    )


class ProviderConfigRow(Base):
    __tablename__ = "providers"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rpm_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rpd_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tpd_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    default_model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    updated_at: Mapped[float] = mapped_column(
        Float, default=time.time, onupdate=time.time, nullable=False
    )

    stats: Mapped[Optional["ProviderStatsRow"]] = relationship(
        back_populates="provider", uselist=False, cascade="all, delete-orphan"
    )


class ProviderStatsRow(Base):
    """Rolling per-provider state. Updated on every call commit/reset."""
    __tablename__ = "provider_stats"

    provider_name: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.name", ondelete="CASCADE"), primary_key=True
    )
    healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quarantined_until: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    last_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ema_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_today: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tokens_day_start: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[float] = mapped_column(
        Float, default=time.time, onupdate=time.time, nullable=False
    )

    provider: Mapped[ProviderConfigRow] = relationship(back_populates="stats")


class RateEventRow(Base):
    """Append-only log of every provider call. Used to compute rolling rpm/rpd.

    Bounded by the periodic purge task (keeps 2 days). BigInteger id for
    high-write consistency with usage_events and client_rate_events.
    """
    __tablename__ = "rate_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider_name: Mapped[str] = mapped_column(
        String(64), ForeignKey("providers.name", ondelete="CASCADE"), nullable=False, index=True
    )
    occurred_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)


class ClientRow(Base):
    """Inbound API client. Raw key is never stored."""
    __tablename__ = "clients"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    rpm_limit: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, default=time.time, nullable=False)


class ClientRateEventRow(Base):
    """Append-only log of inbound calls, one row per authenticated request.

    Mirrors the provider-side rate_events table but with no FK — client hashes
    aren't guaranteed to exist (a revoked client can still have lingering
    events until the purge job runs). The freeai_try_reserve_client plpgsql
    function counts over this table atomically.
    """
    __tablename__ = "client_rate_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[float] = mapped_column(Float, nullable=False)


class UsageEventRow(Base):
    """One row per dispatched completion — the primary source for analytics.

    Stays write-heavy + read-light under normal use; indexes favor time-range
    scans filtered by provider_name.
    """
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fallback_position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    client_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ttfb_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class StrategyRow(Base):
    """A routing strategy — DSL definition decides which providers match.

    The `definition` column is a JSONB document with `require` and `prefer`
    clauses. See app.strategy_dsl for the schema and docs/STRATEGY_DSL.md
    for the design rationale.

    Built-in strategies (auto, fastest, cheapest, ...) are seeded at startup
    with is_builtin=True. Users can add custom ones from the UI; built-ins
    can be edited but not deleted. The special strategy `auto` has
    definition = NULL — it's a hardcoded prompt-inspector, not a data rule.
    """
    __tablename__ = "strategies"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    definition: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[float] = mapped_column(
        Float, default=time.time, onupdate=time.time, nullable=False
    )

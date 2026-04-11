"""Removed in Sprint 2 — the rate tracker moved to Postgres.

See app.repositories.rate_repo.RateRepository. The atomic-reservation logic
that used to live here as a Python lock is now a plpgsql function created by
Alembic migration 0001 (`freeai_try_reserve`).

This file is kept empty to make the move explicit and avoid stale imports.
"""

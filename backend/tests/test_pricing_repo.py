"""Pricing repository — upsert, lookup, cache invalidation, cost compute."""
from __future__ import annotations

import pytest

from app.repositories import ModelPriceDTO, PricingRepository


@pytest.mark.asyncio
async def test_get_missing_returns_none(session):
    repo = PricingRepository(session)
    assert await repo.get("groq", "no-such-model") is None


@pytest.mark.asyncio
async def test_upsert_then_get_round_trips(session):
    repo = PricingRepository(session)
    await repo.upsert(
        ModelPriceDTO(
            provider="groq", model="llama-3.3-70b-versatile",
            input_per_million_usd=0.59, output_per_million_usd=0.79,
        )
    )
    await session.commit()
    PricingRepository.clear_cache()  # force a DB read, not a cache hit

    dto = await repo.get("groq", "llama-3.3-70b-versatile")
    assert dto is not None
    assert dto.input_per_million_usd == 0.59
    assert dto.output_per_million_usd == 0.79
    assert dto.currency == "USD"


@pytest.mark.asyncio
async def test_compute_cost_usd_known_model(session):
    repo = PricingRepository(session)
    await repo.upsert(
        ModelPriceDTO(
            provider="groq", model="llama-3.3-70b-versatile",
            input_per_million_usd=0.59, output_per_million_usd=0.79,
        )
    )
    await session.commit()

    # 1M prompt + 1M completion → 0.59 + 0.79 = 1.38
    cost = await repo.compute_cost_usd(
        "groq", "llama-3.3-70b-versatile",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )
    assert cost == pytest.approx(1.38, rel=1e-6)


@pytest.mark.asyncio
async def test_compute_cost_usd_unknown_model_returns_none(session):
    """Missing price → None, NOT 0.0. The orchestrator records the
    usage_event with cost_usd=NULL so analytics can spot the gap."""
    repo = PricingRepository(session)
    cost = await repo.compute_cost_usd(
        "groq", "unknown-model", prompt_tokens=100, completion_tokens=100,
    )
    assert cost is None


@pytest.mark.asyncio
async def test_compute_cost_usd_with_no_model_returns_none(session):
    """An error path that recorded model=None (e.g. provider rejected the
    request before we knew which model was used) must not crash pricing."""
    repo = PricingRepository(session)
    cost = await repo.compute_cost_usd(
        "groq", None, prompt_tokens=100, completion_tokens=100,
    )
    assert cost is None


@pytest.mark.asyncio
async def test_upsert_invalidates_cache(session):
    """A price update must take effect immediately for the same worker,
    not after the 60 s TTL — otherwise admin edits feel laggy."""
    repo = PricingRepository(session)
    await repo.upsert(
        ModelPriceDTO(
            provider="groq", model="m1",
            input_per_million_usd=1.0, output_per_million_usd=1.0,
        )
    )
    await session.commit()

    # Prime the cache with the first price.
    await repo.get("groq", "m1")
    # Now update.
    await repo.upsert(
        ModelPriceDTO(
            provider="groq", model="m1",
            input_per_million_usd=2.0, output_per_million_usd=2.0,
        )
    )
    await session.commit()

    dto = await repo.get("groq", "m1")
    assert dto is not None
    assert dto.input_per_million_usd == 2.0


@pytest.mark.asyncio
async def test_delete_removes_and_invalidates(session):
    repo = PricingRepository(session)
    await repo.upsert(
        ModelPriceDTO(
            provider="groq", model="m2",
            input_per_million_usd=1.0, output_per_million_usd=1.0,
        )
    )
    await session.commit()
    assert await repo.get("groq", "m2") is not None

    deleted = await repo.delete("groq", "m2")
    await session.commit()
    assert deleted is True
    assert await repo.get("groq", "m2") is None


@pytest.mark.asyncio
async def test_list_all_orders_by_provider_then_model(session):
    repo = PricingRepository(session)
    for prov, model in [("groq", "b"), ("groq", "a"), ("gemini", "z")]:
        await repo.upsert(
            ModelPriceDTO(
                provider=prov, model=model,
                input_per_million_usd=0.1, output_per_million_usd=0.1,
            )
        )
    await session.commit()

    rows = await repo.list_all()
    assert [(r.provider, r.model) for r in rows] == [
        ("gemini", "z"), ("groq", "a"), ("groq", "b"),
    ]


# ──────────────── Compute-only unit tests (no DB) ────────────────


def test_dto_compute_cost_round_numbers():
    dto = ModelPriceDTO(
        provider="x", model="y",
        input_per_million_usd=10.0, output_per_million_usd=20.0,
    )
    # 100k prompt tokens at $10/M → $1.00 ; 50k completion at $20/M → $1.00
    assert dto.compute_cost_usd(100_000, 50_000) == pytest.approx(2.0)


def test_dto_compute_cost_zero_pricing_is_free_tier():
    dto = ModelPriceDTO(
        provider="x", model="y",
        input_per_million_usd=0.0, output_per_million_usd=0.0,
    )
    assert dto.compute_cost_usd(1_000_000, 1_000_000) == 0.0


def test_seed_list_covers_every_known_provider():
    """Migration 0020 seeds at least one row per provider in KNOWN_MODELS.
    A regression here would mean rolling out a new provider without
    pricing — every dispatch records cost_usd=NULL until an admin fixes it.

    The migration filename starts with a digit (not a valid Python module
    name), so load it from the file path via importlib.util."""
    import importlib.util
    from pathlib import Path
    from app.providers.known_models import KNOWN_MODELS

    path = (
        Path(__file__).parent.parent
        / "alembic" / "versions" / "0020_model_pricing.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0020", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    seed_providers = {row[0] for row in migration._SEED_PRICES}
    assert set(KNOWN_MODELS.keys()) <= seed_providers, (
        f"missing seed coverage for providers: "
        f"{set(KNOWN_MODELS.keys()) - seed_providers}"
    )

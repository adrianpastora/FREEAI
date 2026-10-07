"""Config repository — encryption at the boundary, upsert semantics, defaults."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ProviderConfigRow
from app.repositories import ConfigRepository, ProviderConfigDTO


@pytest.mark.asyncio
async def test_seed_defaults_idempotent(session):
    repo = ConfigRepository(session)
    added_first = await repo.seed_defaults_if_empty()
    await session.commit()
    added_second = await repo.seed_defaults_if_empty()
    await session.commit()
    assert added_first > 0
    assert added_second == 0


@pytest.mark.asyncio
async def test_provider_keys_persisted_encrypted(seeded_session):
    repo = ConfigRepository(seeded_session)
    await repo.patch_provider("groq", api_key="sk-secret-99")
    await seeded_session.commit()

    # Read raw row directly to verify on-disk format
    row = await seeded_session.get(ProviderConfigRow, "groq")
    assert row.api_key_encrypted is not None
    assert row.api_key_encrypted.startswith("enc::")
    assert "sk-secret-99" not in row.api_key_encrypted


@pytest.mark.asyncio
async def test_get_provider_returns_decrypted_key(seeded_session):
    repo = ConfigRepository(seeded_session)
    await repo.patch_provider("groq", api_key="sk-secret-77")
    await seeded_session.commit()
    dto = await repo.get_provider("groq")
    assert dto.api_key == "sk-secret-77"


@pytest.mark.asyncio
async def test_patch_provider_unknown_raises(seeded_session):
    repo = ConfigRepository(seeded_session)
    with pytest.raises(KeyError):
        await repo.patch_provider("does-not-exist", api_key="x")


@pytest.mark.asyncio
async def test_app_config_round_trip(session):
    repo = ConfigRepository(session)
    await repo.seed_defaults_if_empty()
    await session.commit()

    cfg = await repo.get_app_config()
    assert cfg.default_strategy == "auto"
    assert cfg.enable_fallback is True

    await repo.set_strategy("coding")
    await repo.set_fallback(False)
    await session.commit()

    cfg = await repo.get_app_config()
    assert cfg.default_strategy == "coding"
    assert cfg.enable_fallback is False


# ──────────────── catalog sync ────────────────


@pytest.mark.asyncio
async def test_sync_catalog_refreshes_stale_install(seeded_session):
    """Simulate an install seeded with the 2026-04 catalog: retired Groq
    default, a user pinned to a retired model, no NVIDIA row. One sync
    brings it up to date; a second is a no-op."""
    from app.db.models import AppConfigRow, ModelPriceRow, UserProviderRow
    from app.providers.catalog import CATALOG, CATALOG_VERSION
    from app.repositories.user_provider_repo import UserProviderRepository

    session = seeded_session
    repo = ConfigRepository(session)
    await repo.get_app_config()
    await repo.patch_provider("groq", default_model="llama-3.3-70b-versatile", rpd_limit=14_400)
    nvidia = await session.get(ProviderConfigRow, "nvidia")
    await session.delete(nvidia)
    await UserProviderRepository(session).upsert(
        1, "groq", api_key="k", default_model="llama-3.3-70b-versatile",
    )
    await UserProviderRepository(session).upsert(
        1, "mistral", api_key="k", default_model="mistral-small-latest",
    )
    await session.commit()

    result = await repo.sync_catalog()
    await session.commit()
    assert result.applied
    assert "nvidia" in result.providers_added
    assert "groq" in result.providers_updated
    assert result.user_overrides_cleared == 1

    groq = await repo.get_provider("groq")
    assert groq.default_model == CATALOG["groq"].default_model
    assert groq.rpd_limit == CATALOG["groq"].rpd_limit
    assert await session.get(ProviderConfigRow, "nvidia") is not None

    overrides = {
        r.provider_name: r.default_model
        for r in (await session.execute(select(UserProviderRow))).scalars()
    }
    assert overrides["groq"] is None                         # retired → cleared
    assert overrides["mistral"] == "mistral-small-latest"    # still valid → kept

    price = await session.get(
        ModelPriceRow, ("openrouter", CATALOG["openrouter"].default_model),
    )
    assert price is not None and price.input_price_per_million_usd == 0.0
    cfg = await session.get(AppConfigRow, 1)
    assert cfg.catalog_version == CATALOG_VERSION

    again = await repo.sync_catalog()
    assert again.applied is False


@pytest.mark.asyncio
async def test_sync_catalog_keeps_admin_tuned_prices(seeded_session):
    from app.db.models import AppConfigRow, ModelPriceRow
    from app.providers.catalog import CATALOG

    session = seeded_session
    model = CATALOG["mistral"].default_model
    session.add(ModelPriceRow(
        provider_name="mistral", model=model,
        input_price_per_million_usd=9.0, output_price_per_million_usd=9.0,
    ))
    await ConfigRepository(session).get_app_config()
    cfg = await session.get(AppConfigRow, 1)
    cfg.catalog_version = None
    await session.commit()

    await ConfigRepository(session).sync_catalog()
    await session.commit()
    price = await session.get(ModelPriceRow, ("mistral", model))
    assert price.input_price_per_million_usd == 9.0

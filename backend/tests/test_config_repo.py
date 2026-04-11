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

"""Virtual models — registry, resolution, and end-to-end routing."""
from __future__ import annotations

import pytest

from app.providers.base import ProviderResponse
from app.repositories import (
    ConfigRepository,
    ProviderConfigDTO,
    RateRepository,
    StrategyRepository,
    UsageRepository,
)
from app.repositories.user_provider_repo import UserProviderRepository
from app.repositories.user_repo import UserRepository
from app.orchestrator import Orchestrator
from app.schemas import ChatCompletionRequest, ChatMessage
from app.virtual_models import (
    DEFAULT_VIRTUAL_MODEL,
    VIRTUAL_MODEL_MAP,
    VIRTUAL_MODELS,
    is_virtual_model,
    resolve_virtual_model,
)


# ──────────────── registry unit tests ────────────────


def test_all_virtual_models_have_unique_ids():
    ids = [vm.id for vm in VIRTUAL_MODELS]
    assert len(ids) == len(set(ids))


def test_is_virtual_model_positive():
    assert is_virtual_model("freeai-auto") is True
    assert is_virtual_model("freeai-fast") is True
    assert is_virtual_model("freeai-code") is True


def test_is_virtual_model_negative():
    assert is_virtual_model(None) is False
    assert is_virtual_model("gpt-4") is False
    assert is_virtual_model("llama-3.3-70b-versatile") is False


def test_resolve_virtual_model():
    vm = resolve_virtual_model("freeai-fast")
    assert vm.strategy == "fastest"


def test_resolve_virtual_model_unknown_raises():
    with pytest.raises(KeyError):
        resolve_virtual_model("gpt-4")


def test_default_virtual_model_is_auto():
    assert DEFAULT_VIRTUAL_MODEL == "freeai-auto"


def test_every_virtual_model_maps_to_a_builtin_strategy():
    """Each virtual model must reference one of the seeded builtin strategies."""
    from app.repositories.strategy_repo import BUILTIN_STRATEGIES
    builtin_names = {s.name for s in BUILTIN_STRATEGIES}
    for vm in VIRTUAL_MODELS:
        assert vm.strategy in builtin_names, (
            f"virtual model '{vm.id}' references strategy '{vm.strategy}' "
            f"which is not in BUILTIN_STRATEGIES"
        )


# ──────────────── orchestrator integration ────────────────


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, *, name, response):
        self.name = name
        self._response = response
        self.calls = 0
        self.last_model = None

    async def complete(self, messages, *, model, temperature, max_tokens, client):
        self.calls += 1
        self.last_model = model
        return self._response


async def _setup(session):
    from app.auth import hash_password
    config_repo = ConfigRepository(session)
    strategy_repo = StrategyRepository(session)
    user_repo = UserRepository(session)
    user_provider_repo = UserProviderRepository(session)

    if (await user_repo.count()) == 0:
        await user_repo.create("vmuser", hash_password("testpass123"), role="admin")
    user = await user_repo.find_by_username("vmuser")
    user_id = user.id

    await config_repo.upsert_provider(ProviderConfigDTO(
        name="primary", api_key=None, enabled=True,
        tags=["fast", "coding", "quality", "reasoning", "vision", "long_context", "cheap"],
        rpm_limit=100, rpd_limit=1000, weight=1.0,
    ))
    await user_provider_repo.upsert(user_id, "primary", api_key="x", enabled=True)
    await config_repo.get_app_config()
    await strategy_repo.seed_builtins_if_missing()
    await session.commit()

    fake = FakeProvider(
        name="primary",
        response=ProviderResponse(
            content="hello", model="real-model-v2", provider="primary",
        ),
    )
    orch = Orchestrator()
    orch._build_provider = lambda dto: fake
    return orch, fake, user_id


async def _run_chat(orch, session, req, user_id):
    return await orch.chat(
        req,
        user_id,
        UserProviderRepository(session),
        ConfigRepository(session),
        RateRepository(session),
        UsageRepository(session),
        StrategyRepository(session),
    )


@pytest.mark.asyncio
async def test_virtual_model_overrides_strategy(session):
    """Sending model=freeai-fast should use 'fastest' strategy."""
    orch, fake, user_id = await _setup(session)
    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="freeai-fast",
        ),
        user_id,
    )
    await session.commit()
    assert res.model == "freeai-fast"
    assert res.real_model == "real-model-v2"
    assert res.strategy_used == "fastest"
    # model should have been set to None for the provider
    assert fake.last_model is None


@pytest.mark.asyncio
async def test_virtual_model_auto(session):
    orch, fake, user_id = await _setup(session)
    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hello world")],
            model="freeai-auto",
        ),
        user_id,
    )
    await session.commit()
    assert res.model == "freeai-auto"
    assert res.real_model == "real-model-v2"


@pytest.mark.asyncio
async def test_passthrough_model_unchanged(session):
    """Non-virtual model names pass through to the provider as-is."""
    orch, fake, user_id = await _setup(session)
    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hi")],
            model="llama-3.3-70b-versatile",
        ),
        user_id,
    )
    await session.commit()
    # model in response is the real one (no virtual override)
    assert res.model == "real-model-v2"
    assert res.real_model is None
    assert fake.last_model == "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_no_model_uses_default_virtual(session):
    """When model is None and strategy is auto, it behaves as freeai-auto."""
    orch, fake, user_id = await _setup(session)
    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hi")],
        ),
        user_id,
    )
    await session.commit()
    assert res.model == "freeai-auto"
    assert res.real_model == "real-model-v2"

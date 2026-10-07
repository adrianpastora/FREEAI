"""Tests for the orchestrator streaming path.

Mocks a provider to yield StreamChunks and validates that the orchestrator's
stream() method produces the expected SSE-shaped dicts, records usage events
with token counts and TTFB, and handles failures correctly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from app.orchestrator import Orchestrator
from app.providers.base import ProviderError, ErrorKind, StreamChunk
from app.repositories import (
    ConfigRepository,
    RateRepository,
    StrategyRepository,
    UsageRepository,
)
from app.repositories.user_provider_repo import UserProviderRepository
from app.repositories.user_repo import UserRepository
from app.schemas import ChatCompletionRequest, ChatMessage


@pytest_asyncio.fixture
async def repos(seeded_session):
    session = seeded_session
    config_repo = ConfigRepository(session)
    rate_repo = RateRepository(session)
    usage_repo = UsageRepository(session)
    strategy_repo = StrategyRepository(session)
    user_provider_repo = UserProviderRepository(session)
    user_repo = UserRepository(session)
    user = await user_repo.find_by_username("testadmin")
    user_id = user.id
    await strategy_repo.seed_builtins_if_missing()
    await session.commit()
    return (
        config_repo,
        rate_repo,
        usage_repo,
        strategy_repo,
        user_provider_repo,
        user_id,
        session,
    )


def _make_req(prompt="Hello", strategy="fastest", stream=True):
    return ChatCompletionRequest(
        messages=[ChatMessage(role="user", content=prompt)],
        strategy=strategy,
        stream=stream,
    )


async def _fake_stream(*args, **kwargs):
    yield StreamChunk(delta="Hello", provider="groq", model="test-model")
    yield StreamChunk(delta=" world", provider="groq", model="test-model")
    yield StreamChunk(
        delta="", provider="groq", model="test-model",
        finish_reason="stop",
        prompt_tokens=10, completion_tokens=5,
    )


@pytest.mark.asyncio
async def test_stream_yields_chunks_and_done(repos):
    config_repo, rate_repo, usage_repo, strategy_repo, user_provider_repo, user_id, session = repos

    await user_provider_repo.upsert(user_id, "groq", api_key="test-key", enabled=True)
    await session.commit()

    orch = Orchestrator()
    req = _make_req()

    with patch.object(
        type(orch._client), "stream", side_effect=NotImplementedError
    ):
        from app.providers import PROVIDER_REGISTRY
        provider_cls = PROVIDER_REGISTRY["groq"]
        with patch.object(provider_cls, "stream", _fake_stream):
            chunks = []
            async for chunk in orch.stream(
                req, user_id, user_provider_repo,
                config_repo, rate_repo, usage_repo, strategy_repo,
            ):
                chunks.append(chunk)

    await orch.aclose()

    assert len(chunks) >= 3
    content_chunks = [c for c in chunks if c.get("choices", [{}])[0].get("delta", {}).get("content")]
    assert len(content_chunks) >= 1
    last = chunks[-1]
    assert last["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_stream_records_usage_with_tokens(repos):
    config_repo, rate_repo, usage_repo, strategy_repo, user_provider_repo, user_id, session = repos

    await user_provider_repo.upsert(user_id, "groq", api_key="test-key", enabled=True)
    await session.commit()

    orch = Orchestrator()
    req = _make_req()

    from app.providers import PROVIDER_REGISTRY
    provider_cls = PROVIDER_REGISTRY["groq"]
    with patch.object(provider_cls, "stream", _fake_stream):
        async for _ in orch.stream(
            req, user_id, user_provider_repo,
            config_repo, rate_repo, usage_repo, strategy_repo,
        ):
            pass

    await session.commit()
    await orch.aclose()

    from sqlalchemy import text
    result = await session.execute(
        text("SELECT prompt_tokens, completion_tokens, ttfb_ms FROM usage_events ORDER BY id DESC LIMIT 1")
    )
    row = result.one_or_none()
    assert row is not None
    assert row[0] == 10
    assert row[1] == 5
    assert row[2] is not None and row[2] >= 0


@pytest.mark.asyncio
async def test_stream_all_fail_raises(repos):
    config_repo, rate_repo, usage_repo, strategy_repo, user_provider_repo, user_id, session = repos

    await user_provider_repo.upsert(user_id, "groq", api_key="test-key", enabled=True)
    await session.commit()

    async def _fail_stream(*args, **kwargs):
        raise ProviderError("groq", "test error", kind=ErrorKind.SERVER_ERROR)
        yield  # pragma: no cover

    orch = Orchestrator()
    req = _make_req()

    from app.providers import PROVIDER_REGISTRY
    provider_cls = PROVIDER_REGISTRY["groq"]
    with patch.object(provider_cls, "stream", _fail_stream):
        with pytest.raises(ProviderError):
            async for _ in orch.stream(
                req, user_id, user_provider_repo,
                config_repo, rate_repo, usage_repo, strategy_repo,
            ):
                pass

    await orch.aclose()


# ──────────────── model / provider fallback while streaming ────────────────


@pytest.mark.asyncio
async def test_stream_falls_through_retired_model(repos):
    """Default model retired upstream → next catalog model streams instead."""
    from app.providers import PROVIDER_REGISTRY
    from app.providers.catalog import CATALOG, model_chain

    config_repo, rate_repo, usage_repo, strategy_repo, user_provider_repo, user_id, session = repos
    await user_provider_repo.upsert(user_id, "groq", api_key="test-key", enabled=True)
    await session.commit()

    retired = CATALOG["groq"].default_model
    replacement = model_chain("groq", None)[1]
    seen_models = []

    async def model_aware_stream(self, messages, *, model=None, **kwargs):
        seen_models.append(model)
        if model == retired:
            raise ProviderError("groq", "model decommissioned", kind=ErrorKind.MODEL_UNAVAILABLE)
        yield StreamChunk(delta="hi", provider="groq", model=model)
        yield StreamChunk(delta="", provider="groq", model=model, finish_reason="stop")

    orch = Orchestrator()
    with patch.object(PROVIDER_REGISTRY["groq"], "stream", model_aware_stream):
        chunks = [c async for c in orch.stream(
            _make_req(), user_id, user_provider_repo,
            config_repo, rate_repo, usage_repo, strategy_repo,
        )]
    await orch.aclose()
    await session.commit()

    assert seen_models == [retired, replacement]
    assert chunks[0]["choices"][0]["delta"]["content"] == "hi"
    snap = await rate_repo.snapshot(user_id, "groq")
    assert snap.healthy is True  # one dead model doesn't park the provider


@pytest.mark.asyncio
async def test_stream_unexpected_exception_falls_back(repos):
    """An adapter bug (non-ProviderError) before the first chunk must fall
    back to the next provider instead of killing the stream."""
    from app.providers import PROVIDER_REGISTRY

    config_repo, rate_repo, usage_repo, strategy_repo, user_provider_repo, user_id, session = repos
    await user_provider_repo.upsert(user_id, "groq", api_key="k", enabled=True)
    await user_provider_repo.upsert(user_id, "mistral", api_key="k", enabled=True)
    await session.commit()

    async def broken_stream(self, messages, **kwargs):
        raise IndexError("list index out of range")
        yield  # pragma: no cover

    async def ok_stream(self, messages, *, model=None, **kwargs):
        yield StreamChunk(delta="rescued", provider="mistral", model=model or "m")

    req = _make_req()
    req.preferred_provider = "groq"
    orch = Orchestrator()
    with patch.object(PROVIDER_REGISTRY["groq"], "stream", broken_stream), \
         patch.object(PROVIDER_REGISTRY["mistral"], "stream", ok_stream):
        chunks = [c async for c in orch.stream(
            req, user_id, user_provider_repo,
            config_repo, rate_repo, usage_repo, strategy_repo,
        )]
    await orch.aclose()
    await session.commit()

    contents = [c["choices"][0]["delta"].get("content") for c in chunks]
    assert "rescued" in contents
    assert chunks[0]["provider"] == "mistral"

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

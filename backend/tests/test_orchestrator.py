"""Orchestrator — strategy detection, fallback chain, error classification."""
from __future__ import annotations

from typing import Optional
from unittest.mock import patch

import pytest

from app.auto_strategy import detect_auto_strategy
from app.orchestrator import Orchestrator
from app.providers.base import ErrorKind, ProviderError, ProviderResponse
from app.repositories import (
    ConfigRepository,
    ProviderConfigDTO,
    RateRepository,
    StrategyRepository,
    UsageRepository,
)
from app.schemas import ChatCompletionRequest, ChatMessage


# ──────────────── helpers ────────────────


class FakeProvider:
    name = "fake"
    supports_streaming = True

    def __init__(self, *, name, response=None, error=None):
        self.name = name
        self._response = response
        self._error = error
        self.calls = 0

    async def complete(self, messages, *, model, temperature, max_tokens, client):
        self.calls += 1
        if self._error:
            raise self._error
        return self._response


def _make_response(provider: str, content: str = "ok") -> ProviderResponse:
    return ProviderResponse(content=content, model="test-model", provider=provider)


def _make_error(provider: str, kind: ErrorKind, message: str = "boom") -> ProviderError:
    return ProviderError(provider, message, kind=kind)


async def _setup_two_providers(session, primary_provider, secondary_provider):
    """Insert two providers + seed strategies, return an orchestrator that
    builds them as our fakes via a patched _build_provider."""
    config_repo = ConfigRepository(session)
    strategy_repo = StrategyRepository(session)
    await config_repo.upsert_provider(ProviderConfigDTO(
        name="primary", api_key="x", enabled=True,
        tags=["fast", "coding"], rpm_limit=100, rpd_limit=1000, weight=1.0,
    ))
    await config_repo.upsert_provider(ProviderConfigDTO(
        name="secondary", api_key="x", enabled=True,
        tags=["fast", "coding"], rpm_limit=100, rpd_limit=1000, weight=0.5,
    ))
    await config_repo.get_app_config()
    await strategy_repo.seed_builtins_if_missing()
    await session.commit()

    orch = Orchestrator()
    fakes = {"primary": primary_provider, "secondary": secondary_provider}
    orch._build_provider = lambda dto: fakes.get(dto.name)
    return orch


async def _run_chat(orch, session, req):
    return await orch.chat(
        req,
        ConfigRepository(session),
        RateRepository(session),
        UsageRepository(session),
        StrategyRepository(session),
    )


# ──────────────── orchestrator end-to-end ────────────────


@pytest.mark.asyncio
async def test_returns_response_from_top_provider(session):
    primary = FakeProvider(name="primary", response=_make_response("primary", "hello"))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    await session.commit()
    assert res.provider == "primary"
    assert res.choices[0].message.content == "hello"
    assert res.fallback_chain == ["primary"]
    assert primary.calls == 1
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_falls_back_on_server_error(session):
    primary = FakeProvider(name="primary", error=_make_error("primary", ErrorKind.SERVER_ERROR))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary", "rescue"))
    orch = await _setup_two_providers(session, primary, secondary)

    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    await session.commit()
    assert res.provider == "secondary"
    assert res.choices[0].message.content == "rescue"
    assert res.fallback_chain == ["primary", "secondary"]
    assert primary.calls == 2


@pytest.mark.asyncio
async def test_falls_back_on_rate_limit_without_retry(session):
    primary = FakeProvider(name="primary", error=_make_error("primary", ErrorKind.RATE_LIMITED))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    await session.commit()
    assert res.provider == "secondary"
    assert primary.calls == 1
    snap = await RateRepository(session).snapshot("primary")
    assert snap.healthy is True


@pytest.mark.asyncio
async def test_client_error_does_not_fall_back(session):
    primary = FakeProvider(name="primary", error=_make_error("primary", ErrorKind.CLIENT_ERROR))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    with pytest.raises(ProviderError):
        await _run_chat(
            orch, session,
            ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")]),
        )
    assert secondary.calls == 0


@pytest.mark.asyncio
async def test_no_providers_configured_raises(session):
    """Empty DB → orchestrator can't dispatch."""
    orch = Orchestrator()
    await ConfigRepository(session).get_app_config()
    await StrategyRepository(session).seed_builtins_if_missing()
    await session.commit()
    with pytest.raises(ProviderError) as exc:
        await _run_chat(
            orch, session,
            ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")]),
        )
    assert "no provider" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_preferred_provider_overrides_ranking(session):
    primary = FakeProvider(name="primary", response=_make_response("primary"))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hi")],
            preferred_provider="secondary",
        ),
    )
    await session.commit()
    assert res.provider == "secondary"


# ──────────────── Bug 3: custom strategies end-to-end ────────────────


@pytest.mark.asyncio
async def test_custom_strategy_is_accepted_and_used(session):
    """REVIEW § 6.4: before the fix, ChatCompletionRequest.strategy was
    typed as Literal[...] so pydantic rejected any custom name. After the
    fix it's a plain str; the orchestrator validates by looking up the row."""
    from app.repositories import StrategyDTO

    primary = FakeProvider(name="primary", response=_make_response("primary"))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    # Create a custom strategy that strongly prefers the "coding" tag.
    # In the new DSL, this is a `prefer.contains` clause; the providers
    # both happen to carry tags=["fast","coding"] so they both qualify
    # and the heavy weight breaks ties in favour of coding-tagged ones.
    await StrategyRepository(session).upsert(
        StrategyDTO(
            name="mine",
            definition={
                "require": [],
                "prefer": [
                    {"field": "tags", "op": "contains", "value": "coding", "weight": 5},
                ],
            },
            description="custom",
            is_builtin=False,
        )
    )
    await session.commit()

    res = await _run_chat(
        orch, session,
        ChatCompletionRequest(
            messages=[ChatMessage(role="user", content="hi")],
            strategy="mine",
        ),
    )
    await session.commit()
    # The orchestrator resolved "mine" from the DB and used it
    assert res.strategy_used == "mine"


@pytest.mark.asyncio
async def test_unknown_strategy_raises_client_error(session):
    """Requesting a strategy that doesn't exist should be a clear CLIENT_ERROR,
    not a silent fallback to baseline scoring (which would silently rank by
    weight + headroom + latency without telling the user their strategy is gone)."""
    primary = FakeProvider(name="primary", response=_make_response("primary"))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    with pytest.raises(ProviderError) as exc:
        await _run_chat(
            orch, session,
            ChatCompletionRequest(
                messages=[ChatMessage(role="user", content="hi")],
                strategy="does_not_exist",
            ),
        )
    assert exc.value.kind == ErrorKind.CLIENT_ERROR
    assert "does_not_exist" in str(exc.value)


@pytest.mark.asyncio
async def test_success_writes_usage_event(session):
    """Every dispatched completion should land in the usage_events table."""
    from sqlalchemy import select
    from app.db.models import UsageEventRow

    primary = FakeProvider(name="primary", response=_make_response("primary", "hello"))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    await _run_chat(
        orch, session,
        ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    await session.commit()

    result = await session.execute(select(UsageEventRow))
    events = result.scalars().all()
    assert len(events) == 1
    assert events[0].provider_name == "primary"
    assert events[0].outcome == "success"
    assert events[0].fallback_position == 1


@pytest.mark.asyncio
async def test_fallback_writes_two_usage_events(session):
    """Primary fails transient → fallback → both get usage rows (fail + success)."""
    from sqlalchemy import select
    from app.db.models import UsageEventRow

    primary = FakeProvider(name="primary", error=_make_error("primary", ErrorKind.SERVER_ERROR))
    secondary = FakeProvider(name="secondary", response=_make_response("secondary"))
    orch = await _setup_two_providers(session, primary, secondary)

    await _run_chat(
        orch, session,
        ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")]),
    )
    await session.commit()

    result = await session.execute(select(UsageEventRow).order_by(UsageEventRow.id))
    events = result.scalars().all()
    assert len(events) == 2
    assert events[0].provider_name == "primary"
    assert events[0].outcome == "server_error"
    assert events[0].fallback_position == 1
    assert events[1].provider_name == "secondary"
    assert events[1].outcome == "success"
    assert events[1].fallback_position == 2

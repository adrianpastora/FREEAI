"""Gap 6 — retry budget is honored from AppConfig / per-provider override.

Exercises Orchestrator._try_with_retry directly so we can assert on the number
of attempts made against a failing provider without needing a real database.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.orchestrator import Orchestrator, _Candidate
from app.providers.base import ErrorKind, ProviderError, ProviderResponse
from app.repositories import ProviderConfigDTO
from app.schemas import ChatCompletionRequest, ChatMessage


class _CountingProvider:
    name = "fake"
    supports_streaming = False

    def __init__(self, *, failures_before_success: int, kind: ErrorKind = ErrorKind.SERVER_ERROR):
        self._left = failures_before_success
        self._kind = kind
        self.calls = 0

    async def complete(self, messages, *, model, temperature, max_tokens, client):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise ProviderError("fake", "boom", kind=self._kind)
        return ProviderResponse(content="ok", model="m", provider="fake")


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=None,
        messages=[ChatMessage(role="user", content="hi")],
        strategy="auto",
        fallback=True,
    )


def _cand(provider, *, max_retries=None) -> _Candidate:
    cfg = ProviderConfigDTO(name="fake", enabled=True, api_key="x", max_retries=max_retries)
    return _Candidate(name="fake", provider=provider, score=1.0, config=cfg)


@pytest.mark.asyncio
async def test_max_retries_zero_means_single_attempt():
    prov = _CountingProvider(failures_before_success=3)
    orch = Orchestrator()
    try:
        result = await orch._try_with_retry(_cand(prov), _req(), None, max_retries=0)
    finally:
        await orch.aclose()
    assert prov.calls == 1
    assert result.response is None
    assert result.error is not None and result.error.kind == ErrorKind.SERVER_ERROR


@pytest.mark.asyncio
async def test_max_retries_two_allows_three_attempts_total():
    prov = _CountingProvider(failures_before_success=2)  # will succeed on attempt #3
    orch = Orchestrator()
    try:
        result = await orch._try_with_retry(_cand(prov), _req(), None, max_retries=2)
    finally:
        await orch.aclose()
    assert prov.calls == 3
    assert result.response is not None
    assert result.response.content == "ok"


@pytest.mark.asyncio
async def test_non_transient_error_does_not_retry():
    """AUTH error should never retry even if budget allows it."""
    prov = _CountingProvider(failures_before_success=1, kind=ErrorKind.AUTH)
    orch = Orchestrator()
    try:
        result = await orch._try_with_retry(_cand(prov), _req(), None, max_retries=5)
    finally:
        await orch.aclose()
    assert prov.calls == 1  # no retry for AUTH
    assert result.error is not None and result.error.kind == ErrorKind.AUTH


@pytest.mark.asyncio
async def test_empty_response_is_retried_as_transient():
    """EMPTY_RESPONSE is transient — it should consume the retry budget."""
    prov = _CountingProvider(failures_before_success=1, kind=ErrorKind.EMPTY_RESPONSE)
    orch = Orchestrator()
    try:
        result = await orch._try_with_retry(_cand(prov), _req(), None, max_retries=1)
    finally:
        await orch.aclose()
    assert prov.calls == 2  # retried once, then succeeded
    assert result.response is not None


@pytest.mark.asyncio
async def test_circuit_breaker_kwargs_reads_from_app_cfg():
    """The helper pulls the 4 tunables from AppConfigDTO and ignores None."""
    @dataclass
    class FakeCfg:
        circuit_breaker_threshold: int = 7
        circuit_breaker_window_s: int = 120
        circuit_breaker_base_cooldown_s: int = 15
        circuit_breaker_max_cooldown_s: int = 900
    kw = Orchestrator._circuit_breaker_kwargs(FakeCfg())
    assert kw == {
        "circuit_breaker_threshold": 7,
        "circuit_breaker_window_s": 120,
        "circuit_breaker_base_cooldown_s": 15,
        "circuit_breaker_max_cooldown_s": 900,
    }


def test_circuit_breaker_kwargs_none_returns_empty():
    assert Orchestrator._circuit_breaker_kwargs(None) == {}

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


# ──────────────── Retry delay: Retry-After + jitter ────────────────


def test_retry_delay_honors_retry_after():
    """If the provider gave us a Retry-After hint, use it (capped)."""
    err = ProviderError("p", "slow down", kind=ErrorKind.SERVER_ERROR, retry_after=2.5)
    delay = Orchestrator._retry_delay(err, attempt=0)
    assert delay == 2.5


def test_retry_delay_caps_retry_after_to_max():
    """A 60 s Retry-After hint is clamped — that's quarantine territory."""
    err = ProviderError("p", "slow", kind=ErrorKind.SERVER_ERROR, retry_after=60.0)
    delay = Orchestrator._retry_delay(err, attempt=0)
    assert delay == Orchestrator._RETRY_AFTER_MAX_S


def test_retry_delay_falls_back_to_jittered_exponential():
    """Without a Retry-After, use base * 2**attempt * U(0.5, 1.5)."""
    err = ProviderError("p", "boom", kind=ErrorKind.SERVER_ERROR)
    base = Orchestrator._RETRY_BACKOFF_S
    lo, hi = Orchestrator._RETRY_JITTER_RANGE
    # attempt=2 → base * 4 * jitter ∈ [base*4*lo, base*4*hi]
    for _ in range(50):
        delay = Orchestrator._retry_delay(err, attempt=2)
        assert base * 4 * lo <= delay <= base * 4 * hi


def test_retry_delay_ignores_zero_retry_after():
    """retry_after=0 is treated as 'unset' — fall through to exponential."""
    err = ProviderError("p", "boom", kind=ErrorKind.SERVER_ERROR, retry_after=0)
    base = Orchestrator._RETRY_BACKOFF_S
    lo, hi = Orchestrator._RETRY_JITTER_RANGE
    delay = Orchestrator._retry_delay(err, attempt=0)
    assert base * lo <= delay <= base * hi


# ──────────────── Pricing integration: _resolve_cost ────────────────


@pytest.mark.asyncio
async def test_resolve_cost_without_repo_returns_none():
    """No pricing_repo passed → orchestrator records cost_usd=None.

    This is the path taken by callers that haven't been wired up yet (or
    by test code that wants to skip pricing). Must never raise."""
    cost = await Orchestrator._resolve_cost(
        None, provider="groq", model="m", prompt_tokens=100, completion_tokens=100,
    )
    assert cost is None


@pytest.mark.asyncio
async def test_resolve_cost_without_model_returns_none():
    """Error paths record model=None; pricing must short-circuit cleanly."""
    class _StubRepo:
        async def compute_cost_usd(self, *a, **kw):  # pragma: no cover
            raise AssertionError("should not be called when model is None")

    cost = await Orchestrator._resolve_cost(
        _StubRepo(), provider="groq", model=None,
        prompt_tokens=100, completion_tokens=100,
    )
    assert cost is None


@pytest.mark.asyncio
async def test_resolve_cost_swallows_repo_errors():
    """A DB error during pricing must not break recording a real dispatch
    — cost accounting is a secondary concern, not a hard dependency."""
    class _BoomRepo:
        async def compute_cost_usd(self, *a, **kw):
            raise RuntimeError("db down")

    cost = await Orchestrator._resolve_cost(
        _BoomRepo(), provider="groq", model="m",
        prompt_tokens=100, completion_tokens=100,
    )
    assert cost is None


@pytest.mark.asyncio
async def test_resolve_cost_passes_through_repo_value():
    class _FixedRepo:
        async def compute_cost_usd(self, provider, model, prompt_tokens, completion_tokens):
            return 0.42

    cost = await Orchestrator._resolve_cost(
        _FixedRepo(), provider="groq", model="m",
        prompt_tokens=100, completion_tokens=100,
    )
    assert cost == 0.42

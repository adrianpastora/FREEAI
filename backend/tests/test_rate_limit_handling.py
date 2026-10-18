"""Rate-limit awareness: header parsing, per-model cool-downs and proactive
skipping. Pure tests, no DB (httpx.MockTransport + fake providers)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.orchestrator import Orchestrator, _Candidate
from app.providers import catalog
from app.providers.base import (
    ErrorKind,
    ProviderError,
    ProviderResponse,
    RateLimitInfo,
    parse_rate_limit_headers,
    parse_reset,
    retry_after_from_response,
)
from app.providers.openai_compat import OpenAICompatibleProvider
from app.repositories import ProviderConfigDTO
from app.schemas import ChatCompletionRequest, ChatMessage


# ──────────────── header parsing ────────────────


@pytest.mark.parametrize("value,expected", [
    ("26.284", 26.284),          # Cerebras: float seconds
    ("2m59.56s", 179.56),        # Groq: Go duration
    ("7.66s", 7.66),
    ("120ms", 0.12),
    ("1h2m", 3720.0),
    ("garbage", None),
    ("", None),
    (None, None),
])
def test_parse_reset_formats(value, expected):
    got = parse_reset(value)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_parse_reset_epoch_seconds_and_ms():
    now = 1_800_000_000.0
    assert parse_reset(str(now + 30), now=now) == pytest.approx(30)
    assert parse_reset(str(int((now + 45) * 1000)), now=now) == pytest.approx(45)
    assert parse_reset(str(now - 10), now=now) == 0.0  # already past


def test_groq_headers():
    info = parse_rate_limit_headers(httpx.Headers({
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-reset-requests": "2m59.56s",
        "x-ratelimit-remaining-tokens": "5000",
        "x-ratelimit-reset-tokens": "7.66s",
    }))
    assert info.remaining_requests == 0
    assert info.exhausted_for() == pytest.approx(179.56)


def test_cerebras_picks_tightest_bucket():
    info = parse_rate_limit_headers(httpx.Headers({
        "x-ratelimit-remaining-requests-day": "900",
        "x-ratelimit-reset-requests-day": "33011.38",
        "x-ratelimit-remaining-tokens-minute": "0",
        "x-ratelimit-reset-tokens-minute": "26.28",
    }))
    assert info.remaining_requests == 900
    assert info.exhausted_for() == pytest.approx(26.28)


def test_no_headers_means_no_info():
    assert parse_rate_limit_headers(httpx.Headers({"content-type": "application/json"})) is None


def test_quota_left_is_not_exhausted():
    assert RateLimitInfo(remaining_requests=3, remaining_tokens=10).exhausted_for() is None


def test_exhausted_without_reset_uses_default():
    assert RateLimitInfo(remaining_requests=0).exhausted_for() == 60.0


def test_retry_after_prefers_header_then_reset_then_gemini_body():
    assert retry_after_from_response(httpx.Headers({"retry-after": "9"}), "") == 9.0
    assert retry_after_from_response(httpx.Headers({
        "x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "12s",
    }), "") == 12.0
    body = '{"error": {"details": [{"retryDelay": "37s"}]}}'
    assert retry_after_from_response(httpx.Headers(), body) == 37.0
    assert retry_after_from_response(httpx.Headers(), "nope") is None


# ──────────────── adapter plumbing ────────────────


class _OAIStub(OpenAICompatibleProvider):
    name = "oaistub"
    BASE_URL = "https://example.test/v1/chat/completions"


def _ok_body():
    return {
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "model": "stub-model",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.mark.asyncio
async def test_complete_attaches_rate_limit_info():
    def handler(req):
        return httpx.Response(200, json=_ok_body(), headers={
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "30s",
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await _OAIStub(api_key="k", default_model="m").complete([], client=client)
    assert resp.rate_limit is not None
    assert resp.rate_limit.exhausted_for() == 30.0


@pytest.mark.asyncio
async def test_429_retry_after_comes_from_reset_header():
    def handler(req):
        return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={
            "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "4.5s",
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc:
            await _OAIStub(api_key="k", default_model="m").complete([], client=client)
    assert exc.value.kind == ErrorKind.RATE_LIMITED
    assert exc.value.retry_after == 4.5


@pytest.mark.asyncio
async def test_stream_mid_stream_error_chunk_is_classified():
    """OpenRouter sends upstream throttling as an error chunk after HTTP 200."""
    sse = "data: " + json.dumps({"error": {"code": 429, "message": "Rate limit exceeded"}}) + "\n\n"

    def handler(req):
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc:
            async for _ in _OAIStub(api_key="k", default_model="m").stream([], client=client):
                pass
    assert exc.value.kind == ErrorKind.RATE_LIMITED


@pytest.mark.asyncio
async def test_stream_first_chunk_carries_rate_limit():
    sse = (
        "data: " + json.dumps({"choices": [{"delta": {"content": "a"}}], "model": "m"}) + "\n\n"
        "data: " + json.dumps({"choices": [{"delta": {"content": "b"}}], "model": "m"}) + "\n\n"
        "data: [DONE]\n\n"
    )

    def handler(req):
        return httpx.Response(200, content=sse.encode(), headers={
            "content-type": "text/event-stream",
            "x-ratelimit-remaining-requests": "5",
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chunks = [c async for c in _OAIStub(api_key="k", default_model="m").stream([], client=client)]
    assert chunks[0].rate_limit is not None and chunks[0].rate_limit.remaining_requests == 5
    assert chunks[1].rate_limit is None


# ──────────────── orchestrator: per-model cool-downs ────────────────


class _ScriptedProvider:
    """Raises ``errors[model]`` for listed models, succeeds otherwise."""
    supports_streaming = False

    def __init__(self, name, errors=None, rate_limit=None):
        self.name = name
        self.errors = errors or {}
        self.rate_limit = rate_limit
        self.models_called: list = []

    async def complete(self, messages, *, model, temperature, max_tokens, client):
        self.models_called.append(model)
        if model in self.errors:
            raise self.errors[model]
        return ProviderResponse(
            content="ok", model=model or "default", provider=self.name,
            rate_limit=self.rate_limit,
        )


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])


def _cand(provider) -> _Candidate:
    cfg = ProviderConfigDTO(name=provider.name, enabled=True, api_key="x")
    return _Candidate(name=provider.name, provider=provider, score=1.0, config=cfg)


async def _run(orch, prov, user_id=1):
    cand = _cand(prov)
    models = orch._models_for(user_id, cand, None)
    if not models:
        return None
    return await orch._try_models(user_id, cand, _req(), models, max_retries=0)


@pytest.mark.asyncio
async def test_per_model_rate_limit_moves_to_next_model_and_remembers():
    """Groq limits are per model: a 429 on the default should not cost the
    request, and the next request should skip straight to the next model."""
    chain = catalog.model_chain("groq", None)
    first, second = chain[0], chain[1]
    prov = _ScriptedProvider("groq", errors={
        first: ProviderError("groq", "rate limited", kind=ErrorKind.RATE_LIMITED, status=429, retry_after=30),
    })
    orch = Orchestrator()
    try:
        res = await _run(orch, prov)
        assert res.response is not None and res.response.model == second
        assert prov.models_called == [first, second]

        prov.models_called.clear()
        await _run(orch, prov)
        assert prov.models_called == [second]
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_per_key_rate_limit_does_not_burn_other_models():
    """OpenRouter's free quota is per account: trying its other models after
    a 429 would just collect more 429s."""
    first = catalog.model_chain("openrouter", None)[0]
    prov = _ScriptedProvider("openrouter", errors={
        first: ProviderError("openrouter", "free-models-per-day", kind=ErrorKind.QUOTA_EXHAUSTED, status=429),
    })
    orch = Orchestrator()
    try:
        res = await _run(orch, prov)
        assert res.error is not None and res.error.kind == ErrorKind.QUOTA_EXHAUSTED
        assert prov.models_called == [first]
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_402_is_never_per_model():
    first = catalog.model_chain("groq", None)[0]
    prov = _ScriptedProvider("groq", errors={
        first: ProviderError("groq", "payment required", kind=ErrorKind.QUOTA_EXHAUSTED, status=402),
    })
    orch = Orchestrator()
    try:
        res = await _run(orch, prov)
        assert res.error is not None
        assert prov.models_called == [first]
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_every_model_rate_limited_skips_provider_until_cooldown():
    chain = catalog.model_chain("groq", None)
    prov = _ScriptedProvider("groq", errors={
        m: ProviderError("groq", "rl", kind=ErrorKind.RATE_LIMITED, status=429, retry_after=30)
        for m in chain
    })
    orch = Orchestrator()
    try:
        res = await _run(orch, prov)
        assert res.error is not None and res.error.kind == ErrorKind.RATE_LIMITED
        assert prov.models_called == chain
        prov.models_called.clear()
        assert await _run(orch, prov) is None  # whole provider skipped, no call made
        assert prov.models_called == []
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_exhausted_headers_on_success_cool_the_model_proactively():
    chain = catalog.model_chain("groq", None)
    prov = _ScriptedProvider("groq", rate_limit=RateLimitInfo(remaining_requests=0, reset_requests_s=120))
    orch = Orchestrator()
    try:
        res = await _run(orch, prov)
        assert res.response is not None  # this call still succeeded
        prov.rate_limit = None
        prov.models_called.clear()
        await _run(orch, prov)
        assert prov.models_called == [chain[1]]  # default skipped without a 429
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_exhausted_headers_on_per_key_provider_cool_the_provider():
    prov = _ScriptedProvider("openrouter", rate_limit=RateLimitInfo(remaining_requests=0, reset_requests_s=120))
    orch = Orchestrator()
    try:
        await _run(orch, prov)
        prov.models_called.clear()
        assert await _run(orch, prov) is None
        assert prov.models_called == []
        # Another user's quota is untouched.
        assert await _run(orch, prov, user_id=2) is not None
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_cooldown_expires():
    orch = Orchestrator()
    try:
        orch._cooldowns.cool(1, "groq", "m", 0.0, "rate_limited")  # clamps to 1 s
        assert orch._cooldowns.reason(1, "groq", "m") == "rate_limited"
        key = (1, "groq", "m")
        until, reason = orch._cooldowns._data[key]
        orch._cooldowns._data[key] = (until - 5, reason)
        assert orch._cooldowns.reason(1, "groq", "m") is None
    finally:
        await orch.aclose()

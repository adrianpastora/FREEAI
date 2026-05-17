"""Provider-level robustness tests — empty responses, content filtering,
malformed SSE chunks. These don't require a database; they use httpx.MockTransport
to fake provider responses.
"""
from __future__ import annotations

import httpx
import pytest

from app.providers.base import (
    MAX_INLINE_IMAGE_BYTES,
    ErrorKind,
    ProviderError,
    assert_inline_image_within_limit,
    estimate_tokens,
)
from app.providers.gemini_provider import GeminiProvider
from app.providers.openai_compat import OpenAICompatibleProvider


class _OAIStub(OpenAICompatibleProvider):
    name = "oaistub"
    BASE_URL = "https://example.test/v1/chat/completions"


def _openai_response(**overrides) -> dict:
    body = {
        "choices": [
            {
                "message": {"content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "model": "stub-model",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    body["choices"][0].update(overrides.pop("choice", {}))
    body["choices"][0]["message"].update(overrides.pop("message", {}))
    if "finish_reason" in overrides:
        body["choices"][0]["finish_reason"] = overrides["finish_reason"]
    return body


async def _run_openai(handler) -> None:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = _OAIStub(api_key="sk-test", default_model="stub-model")
        return await prov.complete([], client=client)


# ──────────────── Gap 1: empty content → EMPTY_RESPONSE ────────────────


@pytest.mark.asyncio
async def test_openai_empty_content_raises_empty_response():
    def handler(req):
        body = _openai_response()
        body["choices"][0]["message"]["content"] = ""
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderError) as exc:
        await _run_openai(handler)
    assert exc.value.kind == ErrorKind.EMPTY_RESPONSE


@pytest.mark.asyncio
async def test_openai_whitespace_content_raises_empty_response():
    def handler(req):
        body = _openai_response()
        body["choices"][0]["message"]["content"] = "   \n  "
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderError) as exc:
        await _run_openai(handler)
    assert exc.value.kind == ErrorKind.EMPTY_RESPONSE


@pytest.mark.asyncio
async def test_openai_empty_content_with_tool_calls_is_success():
    def handler(req):
        body = _openai_response()
        body["choices"][0]["message"]["content"] = ""
        body["choices"][0]["message"]["tool_calls"] = [
            {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
        ]
        return httpx.Response(200, json=body)

    resp = await _run_openai(handler)
    assert resp.content == ""
    assert resp.provider == "oaistub"


@pytest.mark.asyncio
async def test_openai_null_content_without_tool_calls_raises():
    def handler(req):
        body = _openai_response()
        body["choices"][0]["message"]["content"] = None
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderError) as exc:
        await _run_openai(handler)
    assert exc.value.kind == ErrorKind.EMPTY_RESPONSE


# ──────────────── Gap 2: content_filter → CONTENT_FILTERED ────────────────


@pytest.mark.asyncio
async def test_openai_content_filter_raises_content_filtered():
    def handler(req):
        body = _openai_response()
        body["choices"][0]["finish_reason"] = "content_filter"
        body["choices"][0]["message"]["content"] = ""  # filter usually blanks content
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderError) as exc:
        await _run_openai(handler)
    # content_filter takes priority over EMPTY_RESPONSE detection
    assert exc.value.kind == ErrorKind.CONTENT_FILTERED


@pytest.mark.asyncio
async def test_openai_normal_finish_reason_does_not_flag():
    def handler(req):
        return httpx.Response(200, json=_openai_response())

    resp = await _run_openai(handler)
    assert resp.content == "hello"


# ──────────────── Gap 4: malformed SSE frames counter ────────────────


def _sse_response(frames: list[str]) -> httpx.Response:
    body = "".join(f"data: {f}\n\n" for f in frames)
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


@pytest.mark.asyncio
async def test_openai_stream_tolerates_a_few_bad_frames():
    """3 bad + 1 valid should not raise — parse_errors < 5."""
    valid = '{"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}'
    frames = ["{bad1", "{bad2", "{bad3", valid, "[DONE]"]

    def handler(req):
        return _sse_response(frames)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = _OAIStub(api_key="sk-test", default_model="stub-model")
        out = []
        async for chunk in prov.stream([], client=client):
            out.append(chunk.delta)
    assert "".join(out) == "hi"


@pytest.mark.asyncio
async def test_openai_stream_bails_after_five_bad_frames():
    """5 consecutive bad frames → ProviderError(PARSING)."""
    frames = ["{bad" + str(i) for i in range(6)]

    def handler(req):
        return _sse_response(frames)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = _OAIStub(api_key="sk-test", default_model="stub-model")
        with pytest.raises(ProviderError) as exc:
            async for _ in prov.stream([], client=client):
                pass
        assert exc.value.kind == ErrorKind.PARSING


@pytest.mark.asyncio
async def test_openai_stream_parse_errors_reset_after_good_frame():
    """4 bad, 1 good, 4 more bad → no raise (counter reset between)."""
    good = '{"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}'
    frames = (
        ["{bad"] * 4 + [good] + ["{bad"] * 4 + ["[DONE]"]
    )

    def handler(req):
        return _sse_response(frames)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = _OAIStub(api_key="sk-test", default_model="stub-model")
        deltas = []
        async for chunk in prov.stream([], client=client):
            deltas.append(chunk.delta)
    assert "".join(deltas) == "ok"


@pytest.mark.asyncio
async def test_openai_stream_content_filter_before_content_bails():
    """finish_reason=content_filter before any delta → CONTENT_FILTERED."""
    frames = [
        '{"choices":[{"delta":{},"finish_reason":"content_filter"}]}',
        "[DONE]",
    ]

    def handler(req):
        return _sse_response(frames)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = _OAIStub(api_key="sk-test", default_model="stub-model")
        with pytest.raises(ProviderError) as exc:
            async for _ in prov.stream([], client=client):
                pass
        assert exc.value.kind == ErrorKind.CONTENT_FILTERED


@pytest.mark.asyncio
async def test_openai_stream_content_filter_after_content_propagates():
    """If some deltas already emitted, content_filter should not raise —
    the orchestrator won't fall back mid-response anyway."""
    frames = [
        '{"choices":[{"delta":{"content":"start"},"finish_reason":null}]}',
        '{"choices":[{"delta":{},"finish_reason":"content_filter"}]}',
        "[DONE]",
    ]

    def handler(req):
        return _sse_response(frames)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = _OAIStub(api_key="sk-test", default_model="stub-model")
        out = []
        async for chunk in prov.stream([], client=client):
            out.append((chunk.delta, chunk.finish_reason))
    # must not raise, and we must see the finish_reason propagated
    finishes = [f for _, f in out]
    assert "content_filter" in finishes


# ──────────────── Gemini equivalents ────────────────


def _gemini_response(text: str = "hi", finish: str = "STOP") -> dict:
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
    }


async def _run_gemini(handler) -> object:
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = GeminiProvider(api_key="k", default_model="gemini-2.5-flash")
        return await prov.complete([], client=client)


@pytest.mark.asyncio
async def test_gemini_empty_text_raises_empty_response():
    def handler(req):
        return httpx.Response(200, json=_gemini_response(text=""))

    with pytest.raises(ProviderError) as exc:
        await _run_gemini(handler)
    assert exc.value.kind == ErrorKind.EMPTY_RESPONSE


@pytest.mark.parametrize(
    "blocked",
    ["SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"],
)
@pytest.mark.asyncio
async def test_gemini_blocked_finish_reasons_raise_content_filtered(blocked):
    def handler(req):
        return httpx.Response(200, json=_gemini_response(text="should not matter", finish=blocked))

    with pytest.raises(ProviderError) as exc:
        await _run_gemini(handler)
    assert exc.value.kind == ErrorKind.CONTENT_FILTERED


@pytest.mark.asyncio
async def test_gemini_normal_finish_is_success():
    def handler(req):
        return httpx.Response(200, json=_gemini_response(text="ok", finish="STOP"))

    resp = await _run_gemini(handler)
    assert resp.content == "ok"


# ──────────────── ErrorKind classification — is_transient/is_benign ────────────────


def test_empty_response_is_transient_not_benign():
    """EMPTY_RESPONSE should trigger fallback AND count as health failure."""
    err = ProviderError("p", "empty", kind=ErrorKind.EMPTY_RESPONSE)
    assert err.is_transient is True
    assert err.is_benign is False


def test_content_filtered_is_transient_and_benign():
    """CONTENT_FILTERED triggers fallback but must NOT count as health failure
    (the provider is working fine; it just blocked this specific request)."""
    err = ProviderError("p", "filtered", kind=ErrorKind.CONTENT_FILTERED)
    assert err.is_transient is True
    assert err.is_benign is True


# ──────────────── Inline image size limit ────────────────


def test_inline_image_under_limit_passes():
    # Encoded length under the cap → no exception.
    payload = "A" * 1024  # ~768 bytes decoded — comfortably under 20 MB
    assert_inline_image_within_limit("p", payload) is None


def test_inline_image_over_limit_raises_client_error():
    # 4 base64 chars → 3 decoded bytes. Build a payload that exceeds the cap.
    over_cap = MAX_INLINE_IMAGE_BYTES + (1024 * 1024)  # +1 MB headroom
    payload = "A" * ((over_cap // 3) * 4)
    with pytest.raises(ProviderError) as exc:
        assert_inline_image_within_limit("p", payload)
    assert exc.value.kind == ErrorKind.CLIENT_ERROR
    assert exc.value.status == 413


def test_openai_compat_adapter_rejects_oversized_inline_image_defense_in_depth():
    """Defense in depth: even if the schema layer is bypassed (e.g. a future
    internal caller bypasses Pydantic), the provider adapter's own scan
    catches an oversized inline image and surfaces a CLIENT_ERROR."""
    over_cap_bytes = MAX_INLINE_IMAGE_BYTES + (1024 * 1024)
    huge_b64 = "A" * ((over_cap_bytes // 3) * 4)
    blocks = [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{huge_b64}"}},
    ]
    prov = _OAIStub(api_key="sk-test", default_model="stub-model")
    with pytest.raises(ProviderError) as exc:
        prov._enforce_inline_image_limits(blocks)
    assert exc.value.kind == ErrorKind.CLIENT_ERROR
    assert exc.value.status == 413


def test_openai_compat_adapter_accepts_within_limit_image():
    """Sanity: a small inline image goes through the adapter scan untouched."""
    tiny_b64 = "A" * 1024
    blocks = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_b64}"}},
    ]
    prov = _OAIStub(api_key="sk-test", default_model="stub-model")
    prov._enforce_inline_image_limits(blocks)  # no exception


# ──────────────── Token estimation fallback ────────────────


def test_estimate_tokens_empty_returns_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_short_text_returns_at_least_one():
    # Even a single character should count as 1 token so per-text totals
    # never silently drop to zero.
    assert estimate_tokens("a") == 1


def test_estimate_tokens_scales_with_length():
    # ~4 chars per token; the helper deliberately under-counts vs the
    # real tokenizer so tpd_limit is never stricter than the upstream.
    assert estimate_tokens("a" * 100) == 25


@pytest.mark.asyncio
async def test_openai_compat_estimates_tokens_when_usage_missing():
    """If the upstream omits the usage block, the adapter must fall back
    to a length-based estimate so usage_events stays non-zero."""
    def handler(req):
        body = _openai_response()
        body.pop("usage", None)
        # Make the response content long enough that the estimate is non-trivial.
        body["choices"][0]["message"]["content"] = "x" * 40
        return httpx.Response(200, json=body)

    resp = await _run_openai(handler)
    assert resp.completion_tokens == 10  # 40 chars / 4
    # No input messages were supplied → prompt_tokens is 0 (sum over empty list).
    assert resp.prompt_tokens == 0

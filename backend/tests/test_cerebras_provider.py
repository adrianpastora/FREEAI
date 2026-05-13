"""Smoke test for the Cerebras provider. Verifies that the adapter is wired
into OpenAICompatibleProvider correctly and parses a normal happy-path
response. Empty-content, content-filter, and SSE-parse paths are already
covered by test_provider_robustness.py against the shared base class."""
from __future__ import annotations

import httpx
import pytest

from app.providers import PROVIDER_REGISTRY
from app.providers.cerebras_provider import CerebrasProvider


def test_cerebras_in_registry():
    assert PROVIDER_REGISTRY["cerebras"] is CerebrasProvider


@pytest.mark.asyncio
async def test_cerebras_happy_path():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.cerebras.ai"
        assert req.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-stub",
                "model": "gpt-oss-120b",
                "choices": [
                    {"message": {"content": "hello"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prov = CerebrasProvider(api_key="csk-test", default_model="gpt-oss-120b")
        resp = await prov.complete([], client=client)

    assert resp.content == "hello"
    assert resp.provider == "cerebras"
    assert resp.model == "gpt-oss-120b"

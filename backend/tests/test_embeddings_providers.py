"""Provider adapters — embedding request shape + response parsing.

These tests run without a database: they mock the httpx response so we can
verify the adapter sends the right payload to the right URL and correctly
normalizes the response into an EmbeddingResult.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.providers.base import EmbeddingResult, ErrorKind, ProviderError
from app.providers.gemini_provider import GeminiProvider
from app.providers.mistral_provider import MistralProvider


def _mock_client_returning(status: int, body: dict) -> tuple[MagicMock, AsyncMock]:
    """Build an AsyncClient-like mock whose .post() returns the given payload."""
    response = httpx.Response(status_code=status, content=json.dumps(body).encode())
    post = AsyncMock(return_value=response)
    client = MagicMock()
    client.post = post
    return client, post


@pytest.mark.asyncio
async def test_mistral_embed_sends_openai_shape_and_parses():
    body = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
        ],
        "model": "mistral-embed",
        "usage": {"prompt_tokens": 12, "total_tokens": 12},
    }
    client, post = _mock_client_returning(200, body)
    provider = MistralProvider(api_key="sk-test", default_model=None)

    result = await provider.embed(["hola", "mundo"], client=client)

    # URL + payload
    post.assert_awaited_once()
    _, kwargs = post.call_args
    assert kwargs["json"] == {"model": "mistral-embed", "input": ["hola", "mundo"]}
    assert "Bearer sk-test" in kwargs["headers"]["Authorization"]
    assert post.call_args[0][0].endswith("/v1/embeddings")

    # Response
    assert isinstance(result, EmbeddingResult)
    assert result.vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert result.prompt_tokens == 12
    assert result.provider == "mistral"
    assert result.model == "mistral-embed"


@pytest.mark.asyncio
async def test_mistral_embed_reorders_by_index():
    """Mistral responses may arrive out of order — we sort by `index`."""
    body = {
        "data": [
            {"index": 1, "embedding": [9.0]},
            {"index": 0, "embedding": [1.0]},
        ],
        "model": "mistral-embed",
        "usage": {"prompt_tokens": 2},
    }
    client, _ = _mock_client_returning(200, body)
    provider = MistralProvider(api_key="sk", default_model=None)
    result = await provider.embed(["a", "b"], client=client)
    assert result.vectors == [[1.0], [9.0]]


@pytest.mark.asyncio
async def test_mistral_embed_propagates_auth_error():
    client, _ = _mock_client_returning(401, {"error": {"message": "bad key"}})
    provider = MistralProvider(api_key="sk", default_model=None)
    with pytest.raises(ProviderError) as exc:
        await provider.embed(["x"], client=client)
    assert exc.value.kind == ErrorKind.AUTH


@pytest.mark.asyncio
async def test_mistral_embed_rejects_missing_api_key():
    client = MagicMock()
    provider = MistralProvider(api_key=None, default_model=None)
    with pytest.raises(ProviderError) as exc:
        await provider.embed(["x"], client=client)
    assert exc.value.kind == ErrorKind.AUTH
    client.post.assert_not_called() if hasattr(client.post, "assert_not_called") else None


@pytest.mark.asyncio
async def test_gemini_embed_builds_batch_request_and_parses():
    body = {
        "embeddings": [
            {"values": [0.1, 0.2]},
            {"values": [0.3, 0.4]},
        ],
    }
    client, post = _mock_client_returning(200, body)
    provider = GeminiProvider(api_key="goog-test", default_model=None)

    result = await provider.embed(["hi", "there"], client=client)

    post.assert_awaited_once()
    url = post.call_args[0][0]
    kwargs = post.call_args[1]
    # Default embedding model used + key in query string
    assert ":batchEmbedContents" in url
    assert "text-embedding-004" in url
    assert "key=goog-test" in url
    # Payload is a list of per-text requests with the "models/" prefix
    reqs = kwargs["json"]["requests"]
    assert len(reqs) == 2
    assert reqs[0]["model"] == "models/text-embedding-004"
    assert reqs[0]["content"]["parts"][0]["text"] == "hi"
    assert reqs[1]["content"]["parts"][0]["text"] == "there"

    assert result.vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert result.provider == "gemini"
    assert result.model == "text-embedding-004"
    # Gemini doesn't report token counts for embeddings — the adapter
    # estimates from input length (~chars/4, min 1 per text) so tpd_limit
    # accounting stays honest. Two short inputs → 1+1 = 2 tokens.
    assert result.prompt_tokens == 2


@pytest.mark.asyncio
async def test_gemini_embed_accepts_user_supplied_model():
    body = {"embeddings": [{"values": [1.0]}]}
    client, post = _mock_client_returning(200, body)
    provider = GeminiProvider(api_key="k", default_model=None)
    await provider.embed(["x"], model="text-embedding-005", client=client)
    url = post.call_args[0][0]
    assert "text-embedding-005" in url
    assert post.call_args[1]["json"]["requests"][0]["model"] == "models/text-embedding-005"


@pytest.mark.asyncio
async def test_gemini_embed_propagates_rate_limit():
    client, _ = _mock_client_returning(
        429, {"error": {"message": "quota exceeded"}},
    )
    provider = GeminiProvider(api_key="k", default_model=None)
    with pytest.raises(ProviderError) as exc:
        await provider.embed(["x"], client=client)
    assert exc.value.kind == ErrorKind.RATE_LIMITED

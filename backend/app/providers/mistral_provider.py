"""Mistral La Plateforme — OpenAI-compatible chat completions + embeddings.

Both endpoints speak the OpenAI wire format. Chat reuses OpenAICompatibleProvider
as-is; embeddings is a short standalone method that hits /v1/embeddings.
"""
from __future__ import annotations

from typing import Optional

import httpx

from .base import EmbeddingResult, ErrorKind, ProviderError
from .openai_compat import OpenAICompatibleProvider, _phase_timeout


class MistralProvider(OpenAICompatibleProvider):
    name = "mistral"
    BASE_URL = "https://api.mistral.ai/v1/chat/completions"
    EMBEDDINGS_URL = "https://api.mistral.ai/v1/embeddings"
    DEFAULT_EMBEDDING_MODEL = "mistral-embed"
    supports_embeddings = True
    request_timeout = 60.0

    async def embed(
        self,
        texts: list[str],
        *,
        model: Optional[str] = None,
        client: httpx.AsyncClient,
    ) -> EmbeddingResult:
        if not self.api_key:
            raise ProviderError(self.name, "missing API key", kind=ErrorKind.AUTH)
        chosen = model or self.DEFAULT_EMBEDDING_MODEL
        payload = {"model": chosen, "input": texts}
        headers = {**self._auth_headers(), **self._extra_headers()}
        try:
            resp = await client.post(
                self.EMBEDDINGS_URL, json=payload, headers=headers,
                timeout=_phase_timeout(self.request_timeout),
            )
        except httpx.TimeoutException as e:
            raise ProviderError(self.name, f"timeout: {e}", kind=ErrorKind.NETWORK) from e
        except httpx.HTTPError as e:
            raise ProviderError(self.name, f"network: {e}", kind=ErrorKind.NETWORK) from e
        self._raise_for_status(resp)
        try:
            data = resp.json()
            # OpenAI shape: {"data": [{"embedding": [...], "index": 0}, ...],
            #                "model": "...", "usage": {"prompt_tokens": N, "total_tokens": N}}
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            vectors = [item["embedding"] for item in items]
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise ProviderError(
                self.name, f"unexpected response shape: {e}", kind=ErrorKind.PARSING,
            ) from e
        usage = data.get("usage", {})
        return EmbeddingResult(
            vectors=vectors,
            model=data.get("model", chosen),
            provider=self.name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            raw=data,
        )

"""Text embeddings with multi-provider fallback.

Supported providers (in default priority order):
  1. Mistral  — OpenAI-compatible `/v1/embeddings` (`mistral-embed`, 1024 dim).
  2. Gemini   — `/v1beta/models/{model}:batchEmbedContents` (`text-embedding-004`, 768 dim).

Every provider exposes `BaseProvider.embed(texts, model, client) -> EmbeddingResult`
so the dispatch loop here is a thin reserve → attempt → commit loop that reuses
the rate-limiting, quarantine, and usage-event telemetry paths already in place
for chat and transcription.

Note on model dimensions
────────────────────────
Embeddings are only meaningful when compared to other embeddings from the
**same model**. Different providers return different dimensionalities
(768, 1024, 1536, …), so the response echoes `provider` + `model` fields
callers can use to tag their vector store. If you switch providers mid-stream
your existing index is useless.

No streaming
────────────
Unlike chat, embeddings are always one-shot. Callers get a complete vector
list in a single JSON response.
"""
from __future__ import annotations

from typing import Optional

from .providers import PROVIDER_REGISTRY
from .providers.base import BaseProvider

# Priority order for fallback. First entry is tried first.
EMBEDDING_PROVIDERS: tuple[str, ...] = ("mistral", "gemini")


def supports_embeddings(provider_name: str) -> bool:
    """Whether a given provider has an embeddings implementation.

    Derived from `BaseProvider.supports_embeddings` rather than a duplicate
    list, so adding the capability to a new provider just means setting the
    flag on its class.
    """
    cls = PROVIDER_REGISTRY.get(provider_name)
    if cls is None:
        return False
    return bool(getattr(cls, "supports_embeddings", False))


def build_embedding_provider(
    provider_name: str, api_key: Optional[str], default_model: Optional[str] = None,
) -> BaseProvider:
    """Instantiate a provider adapter for an embeddings call."""
    cls = PROVIDER_REGISTRY[provider_name]
    return cls(api_key=api_key, default_model=default_model)

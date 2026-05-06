"""OpenAI-compatible embeddings with multi-provider fallback.

Supported providers (in default priority order): mistral (1024 dim) →
gemini (768 dim). Embeddings from different models are not comparable;
switching providers requires re-embedding the corpus.
"""
from __future__ import annotations

import time
from typing import Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..embeddings import EMBEDDING_PROVIDERS, build_embedding_provider, supports_embeddings
from ..logging_config import get_logger
from ..orchestrator import Orchestrator
from ..providers import ErrorKind, ProviderError
from ..providers.base import EmbeddingResult
from ..repositories import (
    ProviderConfigDTO,
    RateRepository,
    UsageRepository,
)
from ..repositories.usage_repo import UsageEvent
from ..repositories.user_provider_repo import UserProviderRepository
from ..security import require_client
from ._common import status_for_kind

router = APIRouter(tags=["embeddings"])
log = get_logger("freeai.embeddings")


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    input: Union[str, list[str]] = Field(
        ..., description="String or list of strings to embed.",
    )
    model: Optional[str] = Field(
        default=None,
        description="Embedding model name. Passed verbatim to the chosen "
                    "provider; if omitted, each provider uses its configured "
                    "default (mistral-embed, text-embedding-004, …).",
    )
    preferred_provider: Optional[str] = Field(
        default=None,
        description="Force a specific provider (e.g. 'mistral'). If unset, "
                    "providers are tried in default priority order.",
    )
    fallback: bool = Field(
        default=True,
        description="If True, on transient failures try the next configured "
                    "provider. Set to False to fail fast on the first attempt.",
    )


@router.post("/v1/embeddings")
async def embeddings_endpoint(
    req: EmbeddingRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    client=Depends(require_client),
):
    """OpenAI-compatible embeddings with multi-provider fallback.

    Supported providers (in default priority order):
      1. **mistral** — OpenAI wire format, `mistral-embed` (1024 dim).
      2. **gemini** — Google v1beta, `text-embedding-004` (768 dim).

    Response follows OpenAI's shape:

        {
          "object": "list",
          "data": [{"object": "embedding", "index": 0, "embedding": [...]}, ...],
          "model": "mistral-embed",
          "provider": "mistral",
          "usage": {"prompt_tokens": N, "total_tokens": N},
          "fallback_position": 1
        }

    Embeddings are only comparable to other embeddings from the **same model**.
    If you switch providers you must re-embed your corpus.
    """
    rate_repo = RateRepository(session)
    usage_repo = UsageRepository(session)
    user_provider_repo = UserProviderRepository(session)
    client_hash = client.key_hash if client else None
    user_id = getattr(request.state, "user_id", None)

    if user_id is None:
        raise HTTPException(400, "no user context — authenticate with a client key bound to a user")

    # Normalize input to a list of strings
    texts: list[str] = [req.input] if isinstance(req.input, str) else list(req.input)
    if not texts:
        raise HTTPException(400, "input must be a non-empty string or list of strings")
    if any(not isinstance(t, str) for t in texts):
        raise HTTPException(400, "all input entries must be strings")

    # Build candidate list from the user's configured providers, intersected
    # with the set of providers that implement embeddings. If the caller
    # specified preferred_provider, that one goes first.
    user_providers = await user_provider_repo.list_for_user(user_id)
    priority = list(EMBEDDING_PROVIDERS)
    if req.preferred_provider:
        if req.preferred_provider not in priority:
            raise HTTPException(400, f"provider '{req.preferred_provider}' does not support embeddings")
        priority = [req.preferred_provider] + [p for p in priority if p != req.preferred_provider]

    candidates: list[tuple[str, ProviderConfigDTO]] = []
    for name in priority:
        if not supports_embeddings(name):
            continue
        up = next((p for p in user_providers if p.provider_name == name), None)
        if up and up.api_key and up.enabled:
            dto = Orchestrator._user_provider_to_config(up)
            candidates.append((name, dto))

    if not candidates:
        raise HTTPException(
            400,
            "No embedding provider configured — add an API key for Mistral or Gemini",
        )

    if not req.fallback:
        candidates = candidates[:1]

    errors: list[dict] = []
    fallback_position = 0

    for provider_name, dto in candidates:
        fallback_position += 1

        # Reserve capacity
        reservation = await rate_repo.try_reserve(
            user_id, provider_name, dto.rpm_limit, dto.rpd_limit,
        )
        if reservation is None:
            errors.append({"provider": provider_name, "skipped": "at capacity"})
            continue

        provider = build_embedding_provider(
            provider_name, api_key=dto.api_key, default_model=dto.default_model,
        )
        started = time.time()
        reservation_settled = False
        try:
            try:
                result: EmbeddingResult = await provider.embed(
                    texts, model=req.model, client=request.app.state.orchestrator._client,
                )
            except ProviderError as err:
                latency_ms = int((time.time() - started) * 1000)
                errors.append({
                    "provider": err.provider,
                    "kind": err.kind.value,
                    "message": err.message[:200],
                })

                quarantine_s = None
                if err.kind == ErrorKind.SERVER_ERROR:
                    quarantine_s = 60
                elif err.kind == ErrorKind.NETWORK:
                    quarantine_s = 30

                await rate_repo.commit(
                    reservation, latency_ms, ok=False,
                    error=err.message, error_kind=err.kind.value,
                    quarantine_seconds=quarantine_s,
                )
                reservation_settled = True
                await usage_repo.record(UsageEvent(
                    provider=err.provider, model=req.model or "",
                    strategy="embedding", outcome=err.kind.value,
                    latency_ms=latency_ms, client_hash=client_hash,
                    user_id=user_id, fallback_position=fallback_position,
                ))

                # Auth/client errors won't be fixed by trying another provider
                if err.kind in (ErrorKind.AUTH, ErrorKind.CLIENT_ERROR):
                    break
                continue
        finally:
            if not reservation_settled:
                # Request cancelled or unexpected exception before outcome
                # recorded — release the reservation so rate counters don't
                # carry a phantom in-flight call against the user.
                try:
                    await rate_repo.rollback(reservation)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "embeddings reservation rollback failed",
                        provider=provider_name, exc_info=True,
                    )

        # ── Success ──
        latency_ms = int((time.time() - started) * 1000)
        await rate_repo.commit(
            reservation, latency_ms, ok=True,
            prompt_tokens=result.prompt_tokens, completion_tokens=0,
        )
        await usage_repo.record(UsageEvent(
            provider=result.provider, model=result.model,
            strategy="embedding", outcome="success",
            latency_ms=latency_ms, prompt_tokens=result.prompt_tokens,
            completion_tokens=0, client_hash=client_hash,
            user_id=user_id, fallback_position=fallback_position,
        ))
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": v}
                for i, v in enumerate(result.vectors)
            ],
            "model": result.model,
            "provider": result.provider,
            "usage": {
                "prompt_tokens": result.prompt_tokens,
                "total_tokens": result.prompt_tokens,
            },
            "fallback_position": fallback_position,
        }

    # ── All providers exhausted ──
    last = errors[-1] if errors else {}
    status = status_for_kind(ErrorKind(last["kind"]), 502) if "kind" in last else 503
    raise HTTPException(status, {
        "message": "All embedding providers failed",
        "attempts": errors,
    })

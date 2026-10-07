"""Provider catalog, error classification and per-provider model fallback.
Pure tests, no DB."""
from __future__ import annotations

import datetime as dt

import pytest

from app.orchestrator import Orchestrator, _Candidate
from app.providers import PROVIDER_REGISTRY, catalog
from app.providers.base import (
    AUTH_QUARANTINE_S,
    BILLING_QUOTA_QUARANTINE_S,
    DAILY_QUOTA_QUARANTINE_S,
    MODEL_UNAVAILABLE_QUARANTINE_S,
    RATE_LIMIT_DEFAULT_QUARANTINE_S,
    ErrorKind,
    ProviderError,
    ProviderResponse,
    classify_status,
    quarantine_seconds_for,
)
from app.repositories import ProviderConfigDTO
from app.repositories.config_repo import DEFAULT_PROVIDERS
from app.schemas import ChatCompletionRequest, ChatMessage


# ──────────────── catalog consistency ────────────────


def test_catalog_covers_registry_both_ways():
    assert set(catalog.CATALOG) == set(PROVIDER_REGISTRY)
    assert set(DEFAULT_PROVIDERS) == set(PROVIDER_REGISTRY)


def test_catalog_version_is_a_date():
    dt.date.fromisoformat(catalog.CATALOG_VERSION)


@pytest.mark.parametrize("name", sorted(catalog.CATALOG))
def test_catalog_entry_is_coherent(name):
    entry = catalog.CATALOG[name]
    ids = [m.id for m in entry.models]
    assert ids, f"{name} has no models"
    assert len(ids) == len(set(ids)), f"{name} lists a model twice"
    assert entry.models[0].auto_fallback, f"{name} default model must be auto_fallback"
    assert not set(ids) & set(entry.retired_models), f"{name} lists a retired model"
    assert DEFAULT_PROVIDERS[name].default_model == entry.default_model


def test_model_chain_puts_configured_first_and_dedupes():
    chain = catalog.model_chain("groq", "qwen/qwen3.8-27b")
    assert chain[0] == "qwen/qwen3.8-27b"
    assert chain.count("qwen/qwen3.8-27b") == 1
    assert "openai/gpt-oss-120b" in chain


def test_model_chain_skips_retired_override():
    chain = catalog.model_chain("groq", "llama-3.3-70b-versatile")
    assert "llama-3.3-70b-versatile" not in chain
    assert chain[0] == catalog.CATALOG["groq"].default_model


def test_model_chain_excludes_non_auto_fallback():
    assert "mistral-large-latest" not in catalog.model_chain("mistral", None)
    # ...unless the user picked it explicitly.
    assert catalog.model_chain("mistral", "mistral-large-latest")[0] == "mistral-large-latest"


def test_model_chain_unknown_provider():
    assert catalog.model_chain("nope", None) == []
    assert catalog.model_chain("nope", "m") == ["m"]


# ──────────────── classification ────────────────


@pytest.mark.parametrize("status,body,expected", [
    (404, "No endpoints found for meta-llama/llama-3.3-70b-instruct:free.", ErrorKind.MODEL_UNAVAILABLE),
    (404, "", ErrorKind.MODEL_UNAVAILABLE),
    (400, "The model `llama-3.3-70b-versatile` has been decommissioned and is no longer supported.",
     ErrorKind.MODEL_UNAVAILABLE),
    (400, '{"error": {"code": "model_not_found"}}', ErrorKind.MODEL_UNAVAILABLE),
    (400, "Model meta-llama/Llama-3.2-3B-Instruct is not supported by any provider you have enabled.",
     ErrorKind.MODEL_UNAVAILABLE),
    (429, "Quota exceeded for metric: free_tier_requests, limit: 0, model: gemini-2.5-pro",
     ErrorKind.MODEL_UNAVAILABLE),
    (402, "You have exceeded your monthly included credits for Inference Providers.",
     ErrorKind.QUOTA_EXHAUSTED),
    (403, "Your trial has ended. Please add a payment method to continue.", ErrorKind.QUOTA_EXHAUSTED),
    (429, "Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000.",
     ErrorKind.QUOTA_EXHAUSTED),
    (429, "Too many requests", ErrorKind.RATE_LIMITED),
    (429, "limit: 10, GenerateRequestsPerMinutePerProjectPerModel", ErrorKind.RATE_LIMITED),
    (401, "Invalid API key", ErrorKind.AUTH),
    (400, "max_tokens must be less than 8192", ErrorKind.CLIENT_ERROR),
    (400, "This model's maximum context length is 8192 tokens", ErrorKind.CLIENT_ERROR),
    (503, "overloaded", ErrorKind.SERVER_ERROR),
])
def test_classify_status_uses_body(status, body, expected):
    assert classify_status(status, body) == expected


def _err(kind, status=None, retry_after=None):
    return ProviderError("p", "x", kind=kind, status=status, retry_after=retry_after)


def test_quarantine_policy():
    assert quarantine_seconds_for(_err(ErrorKind.RATE_LIMITED)) == RATE_LIMIT_DEFAULT_QUARANTINE_S
    assert quarantine_seconds_for(_err(ErrorKind.RATE_LIMITED, retry_after=7)) == 7
    assert quarantine_seconds_for(_err(ErrorKind.AUTH)) == AUTH_QUARANTINE_S
    assert quarantine_seconds_for(_err(ErrorKind.QUOTA_EXHAUSTED, status=402)) == BILLING_QUOTA_QUARANTINE_S
    assert quarantine_seconds_for(_err(ErrorKind.QUOTA_EXHAUSTED, status=429)) == DAILY_QUOTA_QUARANTINE_S
    assert quarantine_seconds_for(_err(ErrorKind.MODEL_UNAVAILABLE)) is None
    assert quarantine_seconds_for(
        _err(ErrorKind.MODEL_UNAVAILABLE), all_models_unavailable=True,
    ) == MODEL_UNAVAILABLE_QUARANTINE_S
    assert quarantine_seconds_for(_err(ErrorKind.SERVER_ERROR)) is None


# ──────────────── model fallback inside one provider ────────────────


class _ModelAwareProvider:
    """Fails with MODEL_UNAVAILABLE for models in ``dead``."""
    name = "groq"
    supports_streaming = False

    def __init__(self, dead: set[str]):
        self.dead = dead
        self.models_called: list = []

    async def complete(self, messages, *, model, temperature, max_tokens, client):
        self.models_called.append(model)
        if model in self.dead:
            raise ProviderError("groq", f"model {model} decommissioned", kind=ErrorKind.MODEL_UNAVAILABLE)
        return ProviderResponse(content="ok", model=model or "default", provider="groq")


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(messages=[ChatMessage(role="user", content="hi")])


def _cand(provider, default_model) -> _Candidate:
    cfg = ProviderConfigDTO(name="groq", enabled=True, api_key="x", default_model=default_model)
    return _Candidate(name="groq", provider=provider, score=1.0, config=cfg)


@pytest.mark.asyncio
async def test_retired_default_falls_through_to_next_model_and_is_remembered():
    first = catalog.CATALOG["groq"].default_model
    second = catalog.model_chain("groq", None)[1]
    prov = _ModelAwareProvider(dead={first})
    orch = Orchestrator()
    try:
        cand = _cand(prov, first)
        res = await orch._try_models(1, cand, _req(), orch._models_for(1, cand, None), max_retries=1)
        assert res.response is not None and res.response.model == second
        assert prov.models_called == [first, second]  # MODEL_UNAVAILABLE is not retried

        # Next request skips the dead model without spending a call on it.
        prov.models_called.clear()
        res2 = await orch._try_models(1, cand, _req(), orch._models_for(1, cand, None), max_retries=1)
        assert res2.response is not None
        assert prov.models_called == [second]

        # The memory is per user: another account still probes the default.
        prov.models_called.clear()
        await orch._try_models(2, cand, _req(), orch._models_for(2, cand, None), max_retries=1)
        assert prov.models_called == [first, second]
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_all_models_dead_flags_provider():
    chain = catalog.model_chain("groq", None)
    prov = _ModelAwareProvider(dead=set(chain))
    orch = Orchestrator()
    try:
        cand = _cand(prov, None)
        res = await orch._try_models(1, cand, _req(), orch._models_for(1, cand, None), max_retries=1)
        assert res.error is not None and res.error.kind == ErrorKind.MODEL_UNAVAILABLE
        assert res.all_models_unavailable is True
        assert prov.models_called == chain
    finally:
        await orch.aclose()


@pytest.mark.asyncio
async def test_explicit_model_is_never_substituted():
    prov = _ModelAwareProvider(dead={"some/model"})
    orch = Orchestrator()
    try:
        cand = _cand(prov, None)
        models = orch._models_for(1, cand, "some/model")
        assert models == ["some/model"]
        res = await orch._try_models(1, cand, _req(), models, max_retries=1, explicit_model=True)
        assert res.error is not None
        assert res.all_models_unavailable is False  # client's choice: don't quarantine provider
        assert prov.models_called == ["some/model"]
    finally:
        await orch.aclose()

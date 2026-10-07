"""Provider catalog — the single source of truth for built-in providers.

Free tiers and model line-ups change every few months (models get retired,
free plans start requiring a card). Everything that depends on that churn
lives here, in one place:

  • default limits / weight / tags seeded into the ``providers`` table
  • the ordered model chain per provider (default first, then the models
    the orchestrator falls back to when one comes back as retired/unknown)
  • models known to be retired, so stale user overrides are skipped
  • per-model price hints seeded into ``model_prices``
  • the human-readable free-tier note shown in the panel

How to update (see docs/providers/CATALOG.md):
  1. Edit the entries below.
  2. Bump ``CATALOG_VERSION`` to today's date.
  3. Run ``python scripts/check_catalog.py`` to confirm every model exists.
  4. Restart — ``ConfigRepository.sync_catalog`` pushes the new defaults to
     the database on boot when the stored version differs. No migration
     needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Bump on every edit below. Stored in app_config.catalog_version; a mismatch
# at startup triggers a sync of catalog rows + seed prices.
CATALOG_VERSION = "2026-10-07"


@dataclass(frozen=True)
class CatalogModel:
    id: str
    context_window: Optional[int] = None
    capabilities: tuple[str, ...] = ("chat",)
    note: str = ""
    # When False the model is listed (dropdown / pricing) but never picked
    # automatically as a fallback — e.g. paid-only or expensive SKUs.
    auto_fallback: bool = True
    # USD per million tokens (input, output). None = no price on file.
    price: Optional[tuple[float, float]] = None


@dataclass(frozen=True)
class CatalogProvider:
    name: str
    # Ordered by preference: models[0] is the default model.
    models: tuple[CatalogModel, ...]
    rpm_limit: Optional[int] = None
    rpd_limit: Optional[int] = None
    tpd_limit: Optional[int] = None
    weight: float = 1.0
    tags: tuple[str, ...] = ()
    free_tier: str = ""
    # Models the provider no longer serves. Skipped even if a user pinned
    # them as default_model, and cleared from user overrides on sync.
    retired_models: tuple[str, ...] = ()
    # Date the entry was last checked against the provider's own docs.
    verified: str = CATALOG_VERSION

    @property
    def default_model(self) -> str:
        return self.models[0].id


CATALOG: dict[str, CatalogProvider] = {
    "cerebras": CatalogProvider(
        name="cerebras",
        models=(
            CatalogModel("gpt-oss-120b", 131_000, ("chat", "reasoning")),
        ),
        rpm_limit=30, rpd_limit=14_400, tpd_limit=1_000_000, weight=1.0,
        tags=("fast", "reasoning", "coding", "quality"),
        free_tier=(
            "No permanent free tier since 2026-07-16: $5 trial credit valid "
            "30 days, only after adding a payment method."
        ),
    ),
    "groq": CatalogProvider(
        name="groq",
        models=(
            CatalogModel("openai/gpt-oss-120b", 131_072, ("chat", "reasoning", "tools")),
            CatalogModel("openai/gpt-oss-20b", 131_072, ("chat", "reasoning", "tools"), note="fastest"),
            CatalogModel("qwen/qwen3.8-27b", 131_072, ("chat", "reasoning"), note="preview"),
        ),
        rpm_limit=30, rpd_limit=1_000, tpd_limit=200_000, weight=1.0,
        tags=("fast", "cheap", "coding", "reasoning", "audio"),
        free_tier="Free, no card: 30 req/min, ~1,000 req/day, ~200K tokens/day per model.",
        retired_models=(
            "llama-3.3-70b-versatile", "llama-3.1-70b-versatile",
            "llama-3.1-8b-instant", "mixtral-8x7b-32768", "gemma2-9b-it",
            "qwen/qwen3-32b", "qwen/qwen3.6-27b",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "meta-llama/llama-4-maverick-17b-128e-instruct",
        ),
    ),
    "gemini": CatalogProvider(
        name="gemini",
        models=(
            CatalogModel("gemini-3.5-flash", 1_048_576, ("chat", "vision", "tools", "long_context", "reasoning")),
            CatalogModel("gemini-3.5-flash-lite", 1_048_576, ("chat", "vision", "long_context")),
            CatalogModel("gemini-3-flash-preview", 1_048_576, ("chat", "vision", "tools", "long_context", "reasoning"), note="preview"),
            CatalogModel("gemini-3.1-flash-lite", 1_048_576, ("chat", "vision", "long_context")),
            CatalogModel("gemini-2.5-flash", 1_048_576, ("chat", "vision", "tools", "long_context", "reasoning"),
                         note="legacy: only for projects that already used it", price=(0.30, 2.50)),
        ),
        rpm_limit=10, rpd_limit=250, tpd_limit=None, weight=0.9,
        tags=("quality", "vision", "long_context", "reasoning", "embeddings"),
        free_tier=(
            "Free with a Google account, no card. Flash / Flash-Lite only; "
            "per-project quotas are shown in AI Studio."
        ),
        retired_models=(
            "gemini-2.0-flash", "gemini-2.0-flash-001", "gemini-2.0-flash-lite",
            "gemini-2.0-flash-lite-001", "gemini-2.0-flash-exp",
        ),
    ),
    "mistral": CatalogProvider(
        name="mistral",
        models=(
            CatalogModel("mistral-small-latest", 128_000, ("chat", "tools"), price=(0.20, 0.60)),
            CatalogModel("mistral-medium-latest", 128_000, ("chat", "tools", "reasoning")),
            CatalogModel("codestral-latest", 256_000, ("chat", "coding"), price=(0.30, 0.90)),
            CatalogModel("open-mistral-nemo", 128_000, ("chat",), price=(0.15, 0.15)),
            CatalogModel("mistral-large-latest", 128_000, ("chat", "tools", "reasoning"),
                         auto_fallback=False, price=(2.00, 6.00)),
        ),
        rpm_limit=2, rpd_limit=1_000_000_000, tpd_limit=33_000_000, weight=0.8,
        tags=("coding", "fast", "cheap", "embeddings"),
        free_tier="Free plan, no card: $10/month of API credits, per-project rate caps.",
    ),
    "openrouter": CatalogProvider(
        name="openrouter",
        models=(
            CatalogModel("nvidia/nemotron-3-super-120b-a12b:free", 262_144, ("chat", "reasoning"), price=(0.0, 0.0)),
            CatalogModel("google/gemma-4-31b-it:free", 262_144, ("chat",), price=(0.0, 0.0)),
            CatalogModel("nvidia/nemotron-3-ultra-550b-a55b:free", 1_000_000, ("chat", "reasoning"), price=(0.0, 0.0)),
            CatalogModel("google/gemma-4-26b-a4b-it:free", 262_144, ("chat",), price=(0.0, 0.0)),
        ),
        rpm_limit=20, rpd_limit=50, tpd_limit=None, weight=0.7,
        tags=("quality", "variety", "reasoning"),
        free_tier=(
            "Free, no card: 20 req/min, 50 req/day on ':free' models "
            "(1,000/day once you have bought $10 of credits)."
        ),
        retired_models=(
            "meta-llama/llama-3.3-70b-instruct:free", "meta-llama/llama-3.2-3b-instruct:free",
            "google/gemini-2.0-flash-exp:free", "mistralai/mistral-small-3.1-24b-instruct:free",
            "qwen/qwen-2.5-72b-instruct:free",
        ),
    ),
    "cohere": CatalogProvider(
        name="cohere",
        models=(
            CatalogModel("command-r-08-2024", 128_000, ("chat", "tools", "rag"), price=(0.15, 0.60)),
            CatalogModel("command-r7b-12-2024", 128_000, ("chat",), note="smallest", price=(0.0375, 0.15)),
            CatalogModel("command-a-03-2025", 256_000, ("chat", "tools", "rag", "reasoning"),
                         auto_fallback=False, price=(2.50, 10.00)),
            CatalogModel("command-r-plus-08-2024", 128_000, ("chat", "tools", "rag", "reasoning"),
                         auto_fallback=False, price=(2.50, 10.00)),
        ),
        rpm_limit=20, rpd_limit=33, tpd_limit=None, weight=0.6,
        tags=("fast", "rag"),
        free_tier="Trial key, no card: 1,000 calls/month, 20 req/min on chat.",
    ),
    "huggingface": CatalogProvider(
        name="huggingface",
        models=(
            CatalogModel("openai/gpt-oss-120b", 131_072, ("chat", "reasoning"), price=(0.0, 0.0)),
            CatalogModel("Qwen/Qwen3.6-27B", 131_072, ("chat", "reasoning"), price=(0.0, 0.0)),
            CatalogModel("meta-llama/Llama-3.3-70B-Instruct", 131_000, ("chat",), price=(0.0, 0.0)),
            CatalogModel("meta-llama/Llama-3.1-8B-Instruct", 131_000, ("chat",), note="smallest", price=(0.0, 0.0)),
        ),
        rpm_limit=30, rpd_limit=100, tpd_limit=None, weight=0.5,
        tags=("variety", "cheap"),
        free_tier=(
            "Free account: only $0.10/month of routed credit (HTTP 402 once spent). "
            "PRO accounts get $2/month."
        ),
        retired_models=(
            "meta-llama/Llama-3.2-3B-Instruct", "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ),
    ),
    "nvidia": CatalogProvider(
        name="nvidia",
        models=(
            CatalogModel("nvidia/nemotron-3-super-120b-a12b", 262_144, ("chat", "reasoning"), price=(0.0, 0.0)),
            CatalogModel("deepseek-ai/deepseek-v4.1-flash", 131_072, ("chat", "reasoning", "coding"), price=(0.0, 0.0)),
            CatalogModel("z-ai/glm-5.3-flash", 131_072, ("chat", "coding"), price=(0.0, 0.0)),
            CatalogModel("openai/gpt-oss-20b", 131_072, ("chat", "reasoning"), price=(0.0, 0.0)),
        ),
        rpm_limit=40, rpd_limit=None, tpd_limit=None, weight=0.85,
        tags=("quality", "reasoning", "coding", "variety"),
        free_tier="Free NVIDIA Developer Program, no card: ~40 req/min per model, trial credits on signup.",
    ),
}


def get(provider: str) -> Optional[CatalogProvider]:
    return CATALOG.get(provider)


def is_retired(provider: str, model: Optional[str]) -> bool:
    entry = CATALOG.get(provider)
    return bool(entry and model and model in entry.retired_models)


def model_chain(provider: str, configured: Optional[str]) -> list[str]:
    """Ordered list of models to try for one provider.

    ``configured`` (the user's or catalog row's default_model) goes first
    unless it is known to be retired; then the catalog models flagged
    ``auto_fallback``. Providers outside the catalog get just the
    configured model (or an empty list → adapter picks its own default).
    """
    entry = CATALOG.get(provider)
    chain: list[str] = []
    if configured and not is_retired(provider, configured):
        chain.append(configured)
    if entry:
        for m in entry.models:
            if m.auto_fallback and m.id not in chain:
                chain.append(m.id)
    return chain

"""Virtual model registry — maps FreeAI-branded model names to strategies.

Clients see model names like ``freeai-auto``, ``freeai-fast``, etc. in
``GET /v1/models``. When a request arrives with one of those names in the
``model`` field, the orchestrator resolves the associated strategy and
lets the ranking system pick the real provider + model.

The mapping is intentionally simple: each virtual model is just a
(display name, strategy, description) triple. Adding a new virtual
model only requires adding a row here — the rest of the system already
knows how to execute strategies.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VirtualModel:
    id: str              # what clients send as "model"
    strategy: str        # strategy name to resolve (must exist in strategies table)
    description: str     # shown in /v1/models
    context_window: int = 131_072


# Ordered — first entry is the "default" when no model is specified.
VIRTUAL_MODELS: list[VirtualModel] = [
    VirtualModel(
        id="freeai-auto",
        strategy="auto",
        description="Automatic — FreeAI picks the best provider and model for your prompt.",
    ),
    VirtualModel(
        id="freeai-fast",
        strategy="fastest",
        description="Optimised for low latency. Picks the fastest available provider.",
    ),
    VirtualModel(
        id="freeai-quality",
        strategy="best_quality",
        description="Optimised for quality. Routes to the most capable model available.",
    ),
    VirtualModel(
        id="freeai-code",
        strategy="coding",
        description="Specialised for code generation, review and debugging.",
    ),
    VirtualModel(
        id="freeai-reasoning",
        strategy="reasoning",
        description="Picks providers with strong reasoning and chain-of-thought.",
    ),
    VirtualModel(
        id="freeai-cheap",
        strategy="cheapest",
        description="Minimises cost — prioritises providers with the most remaining quota.",
    ),
    VirtualModel(
        id="freeai-vision",
        strategy="vision",
        description="Routes to providers that support image/vision inputs.",
    ),
    VirtualModel(
        id="freeai-long",
        strategy="long_context",
        description="Routes to providers supporting very long context windows.",
    ),
]

VIRTUAL_MODEL_MAP: dict[str, VirtualModel] = {vm.id: vm for vm in VIRTUAL_MODELS}

DEFAULT_VIRTUAL_MODEL = VIRTUAL_MODELS[0].id


def is_virtual_model(model: str | None) -> bool:
    """Return True if *model* is a FreeAI virtual model name."""
    return model is not None and model in VIRTUAL_MODEL_MAP


def resolve_virtual_model(model: str) -> VirtualModel:
    """Look up a virtual model by id. Raises KeyError if not found."""
    return VIRTUAL_MODEL_MAP[model]

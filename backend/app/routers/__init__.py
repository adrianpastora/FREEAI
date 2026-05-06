"""HTTP routers — one module per resource group.

Each module exports a ``router`` (``APIRouter``) that ``main.py`` mounts
via ``app.include_router(...)``. The split keeps endpoint groups readable
in isolation without touching the orchestration / provider layers.
"""
from . import (
    analytics,
    auth,
    chat,
    clients,
    config,
    embeddings,
    health,
    me_providers,
    providers_admin,
    setup,
    strategies,
    transcriptions,
    users,
)

__all__ = [
    "analytics",
    "auth",
    "chat",
    "clients",
    "config",
    "embeddings",
    "health",
    "me_providers",
    "providers_admin",
    "setup",
    "strategies",
    "transcriptions",
    "users",
]

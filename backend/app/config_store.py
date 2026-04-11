"""Backwards-compat shim — the real implementation moved to app.repositories.config_repo
in Sprint 2 (Postgres). This module only exists so older imports keep working
during the transition. New code should depend on ConfigRepository directly via
the FastAPI dependency injection system."""
from __future__ import annotations

from typing import Optional


def mask_key(key: Optional[str]) -> Optional[str]:
    """Render an API key safely for the UI — keeps first 4 chars + length."""
    if not key:
        return None
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 8}{key[-2:]}"

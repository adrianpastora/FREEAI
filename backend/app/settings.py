"""Centralized settings via pydantic-settings.

Replaces the ad-hoc os.environ.get calls scattered through the codebase.
Reads from environment + .env file. All FREEAI_* prefixed.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FREEAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://freeai:freeai@localhost:5432/freeai",
        description="SQLAlchemy async URL — must use the asyncpg driver",
    )
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    auto_migrate: bool = Field(
        default=True,
        description="Run alembic upgrade head on startup",
    )

    # Auth
    admin_token: Optional[str] = None
    require_auth: bool = False

    # JWT
    jwt_secret: Optional[str] = None
    jwt_secret_path: Path = Path(__file__).parent.parent / "data" / ".jwt_secret"
    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 7

    # Crypto
    master_key: Optional[str] = None
    legacy_initial_setup: bool = Field(
        default=False,
        description=(
            "If true, offer the legacy admin-token + provider-keys wizard when "
            "there are no JWT users. Default false: use master-key confirmation "
            "then JWT registration."
        ),
    )
    master_key_path: Path = Path(__file__).parent.parent / "data" / ".master_key"
    admin_token_path: Path = Path(__file__).parent.parent / "data" / "admin_token"

    # Bootstrap token — protects the initial setup wizard against drive-by takeover.
    # Auto-generated on first run when the instance is fresh and no admin exists.
    # Printed once to stdout; consumed by POST /api/setup/initial or the first
    # /api/auth/register, then deleted.
    bootstrap_token_path: Path = Path(__file__).parent.parent / "data" / ".bootstrap_token"

    # CORS
    cors_origins: str = "http://localhost:8000,http://127.0.0.1:8000"

    # Logging
    log_level: str = "INFO"
    log_format: str = Field(default="console", description="console | json")

    # Provider env-var overrides — kept for backwards compat
    groq_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    mistral_api_key: Optional[str] = None
    openrouter_api_key: Optional[str] = None
    cohere_api_key: Optional[str] = None
    hf_api_key: Optional[str] = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def provider_env_keys(self) -> dict[str, Optional[str]]:
        return {
            "groq": self.groq_api_key,
            "gemini": self.gemini_api_key,
            "mistral": self.mistral_api_key,
            "openrouter": self.openrouter_api_key,
            "cohere": self.cohere_api_key,
            "huggingface": self.hf_api_key,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

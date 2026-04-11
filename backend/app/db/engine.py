"""Async SQLAlchemy engine + sessionmaker. One engine per process."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from ..settings import get_settings

log = logging.getLogger("freeai.db")


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


def create_engine_and_sessionmaker() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    settings = get_settings()
    log.info(
        "creating db engine: %s (pool_size=%d max_overflow=%d)",
        # Don't leak password
        _redact(settings.database_url),
        settings.db_pool_size,
        settings.db_max_overflow,
    )
    engine = create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )
    sessionmaker = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        class_=AsyncSession,
    )
    return engine, sessionmaker


async def dispose_engine(engine: AsyncEngine) -> None:
    await engine.dispose()


def _redact(url: str) -> str:
    """Hide the password from connection string for logging."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" not in creds:
        return url
    user, _ = creds.split(":", 1)
    return f"{scheme}://{user}:***@{host}"

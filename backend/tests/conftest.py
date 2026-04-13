"""Test fixtures for the Postgres-backed app.

Strategy: spin up ONE Postgres container per test session via testcontainers,
run alembic migrations against it, then truncate all tables between tests for
isolation. Truncate is dramatically faster than dropping/recreating the schema
or recycling the container.

If `FREEAI_TEST_DATABASE_URL` is set, we use that instead of starting a
container — useful in CI where you already have a Postgres service.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio


# asyncpg is incompatible with Windows ProactorEventLoop — force Selector.
@pytest.fixture(scope="session")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()

# Bootstrap env vars BEFORE the app modules are imported.
os.environ.setdefault("FREEAI_ADMIN_TOKEN", "adm_test_token")
os.environ.setdefault("FREEAI_MASTER_KEY", "test-master-key-do-not-use-in-prod")
os.environ.setdefault("FREEAI_AUTO_MIGRATE", "false")
os.environ.pop("FREEAI_REQUIRE_AUTH", None)


def _start_test_postgres() -> tuple[str, object]:
    """Returns (sqlalchemy async URL, container handle to stop later)."""
    pre_set = os.environ.get("FREEAI_TEST_DATABASE_URL")
    if pre_set:
        return pre_set, None
    from testcontainers.postgres import PostgresContainer
    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    raw = pg.get_connection_url()  # postgresql+psycopg2://...
    async_url = raw.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    return async_url, pg


@pytest.fixture(scope="session")
def database_url():
    """Lazy: only spin up Postgres when a test actually requests it.
    Tests that don't depend on the DB never trigger the container start."""
    try:
        url, container = _start_test_postgres()
    except Exception as e:
        pytest.skip(f"could not start test postgres: {e}")
    os.environ["FREEAI_DATABASE_URL"] = url
    # invalidate cached settings
    from app.settings import get_settings
    get_settings.cache_clear()
    yield url
    if container is not None:
        container.stop()


@pytest_asyncio.fixture(scope="session")
async def engine(database_url):
    from app.db.engine import create_engine_and_sessionmaker
    eng, _ = create_engine_and_sessionmaker()
    # Run migrations once for the session
    from alembic import command
    from alembic.config import Config as AlembicConfig
    cfg = AlembicConfig(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(scope="session")
async def sessionmaker(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def _truncate_all_tables(url: str) -> None:
    """Run in a worker thread so asyncio.run() is never nested in pytest-asyncio's loop."""

    async def _go() -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import ProgrammingError
        from sqlalchemy.ext.asyncio import create_async_engine

        eng = create_async_engine(url, pool_pre_ping=True)
        try:
            async with eng.begin() as conn:
                await conn.execute(
                    text(
                        "TRUNCATE rate_events, provider_stats, providers, app_config, clients, "
                        "client_rate_events, strategies, usage_events, "
                        "user_providers, refresh_tokens, users RESTART IDENTITY CASCADE"
                    )
                )
        except ProgrammingError as e:
            msg = str(e.orig) if getattr(e, "orig", None) else str(e)
            if "does not exist" not in msg:
                raise
        finally:
            await eng.dispose()

    if sys.platform == "win32":
        loop = asyncio.SelectorEventLoop()
        try:
            loop.run_until_complete(_go())
        finally:
            loop.close()
    else:
        asyncio.run(_go())


@pytest.fixture(autouse=True)
def clean_db(request):
    """Truncate between DB-backed tests without async fixture nesting (Py3.14-safe)."""
    db_fixtures = {"session", "seeded_session", "sessionmaker", "engine", "database_url"}
    if not (set(request.fixturenames) & db_fixtures):
        yield
        return
    url = os.environ.get("FREEAI_DATABASE_URL")
    if not url:
        yield
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_truncate_all_tables, url).result(timeout=120)
    yield


@pytest_asyncio.fixture
async def session(sessionmaker):
    async with sessionmaker() as s:
        yield s
        await s.rollback()


@pytest_asyncio.fixture
async def seeded_session(session):
    """A session with the default providers seeded + an admin user (id=1)."""
    from app.auth import hash_password
    from app.repositories import ConfigRepository
    from app.repositories.user_repo import UserRepository
    config_repo = ConfigRepository(session)
    await config_repo.seed_defaults_if_empty()
    user_repo = UserRepository(session)
    if await user_repo.count() == 0:
        await user_repo.create("testadmin", hash_password("testpass123"), role="admin")
    await session.commit()
    yield session


@pytest_asyncio.fixture
async def admin_user(session):
    """Create and return an admin user for tests."""
    from app.auth import hash_password
    from app.repositories.user_repo import UserRepository
    repo = UserRepository(session)
    user = await repo.create("testadmin", hash_password("testpass123"), role="admin")
    await session.commit()
    return user


@pytest_asyncio.fixture
async def regular_user(session):
    """Create and return a regular user for tests."""
    from app.auth import hash_password
    from app.repositories.user_repo import UserRepository
    repo = UserRepository(session)
    user = await repo.create("testuser", hash_password("testpass123"), role="user")
    await session.commit()
    return user

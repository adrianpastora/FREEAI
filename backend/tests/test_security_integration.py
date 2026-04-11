"""Integration tests for the auth system — require_admin and require_client.

Exercises the full dependency chain: router → security dependency → DB lookup.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.repositories import ClientRepository, ConfigRepository


ADMIN_TOKEN = "adm_test_token"
AUTH_HEADERS = {"X-Admin-Token": ADMIN_TOKEN}


@pytest_asyncio.fixture
async def client(seeded_session, sessionmaker):
    app.state.sessionmaker = sessionmaker
    from app.orchestrator import Orchestrator
    app.state.orchestrator = Orchestrator()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await app.state.orchestrator.aclose()


# ──────────── require_admin ────────────

@pytest.mark.asyncio
async def test_admin_no_token_returns_401(client: AsyncClient):
    resp = await client.get("/api/providers")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_wrong_token_returns_401(client: AsyncClient):
    resp = await client.get(
        "/api/providers",
        headers={"X-Admin-Token": "wrong_token_12345"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_correct_token_succeeds(client: AsyncClient):
    resp = await client.get("/api/providers", headers=AUTH_HEADERS)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_bearer_auth(client: AsyncClient):
    resp = await client.get(
        "/api/providers",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert resp.status_code == 200


# ──────────── require_client (bootstrap mode) ────────────

@pytest.mark.asyncio
async def test_chat_open_in_bootstrap_mode(client: AsyncClient):
    """With no clients configured, /v1/* is open (bootstrap)."""
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "strategy": "fastest",
        },
    )
    # Will fail at the provider level (no API key) but NOT at auth
    assert resp.status_code != 401


# ──────────── require_client (with clients) ────────────

@pytest.mark.asyncio
async def test_chat_requires_key_after_client_created(client: AsyncClient, seeded_session):
    repo = ClientRepository(seeded_session)
    _, raw_key = await repo.create("test-app", 60)
    await seeded_session.commit()

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "strategy": "fastest",
        },
    )
    assert resp.status_code == 401

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "strategy": "fastest",
        },
    )
    assert resp.status_code != 401


@pytest.mark.asyncio
async def test_chat_invalid_key_returns_401(client: AsyncClient, seeded_session):
    repo = ClientRepository(seeded_session)
    await repo.create("test-app2", 60)
    await seeded_session.commit()

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer fai_invalid_key_1234567890abcdef"},
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "strategy": "fastest",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_client_returns_401(client: AsyncClient, seeded_session):
    repo = ClientRepository(seeded_session)
    c, raw_key = await repo.create("revokable", 60)
    await seeded_session.commit()

    await repo.revoke(c.key_hash)
    await seeded_session.commit()

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "strategy": "fastest",
        },
    )
    assert resp.status_code == 401

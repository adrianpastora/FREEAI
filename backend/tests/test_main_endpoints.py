"""End-to-end tests for the FastAPI app via httpx.AsyncClient.

These hit the real router (dependency injection, middleware, error mapping) but
mock the provider layer to avoid external HTTP calls.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.repositories import ConfigRepository, StrategyRepository


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


# ──────────── health ────────────

@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "providers_configured" in data
    assert "auth_required" in data


# ──────────── setup ────────────

@pytest.mark.asyncio
async def test_setup_status_returns_false_when_token_set(client: AsyncClient):
    resp = await client.get("/api/setup/status")
    assert resp.status_code == 200
    assert resp.json()["needs_initial_setup"] is False


# ──────────── providers (admin auth) ────────────

@pytest.mark.asyncio
async def test_providers_requires_admin(client: AsyncClient):
    resp = await client.get("/api/providers")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_providers_list_with_admin_token(client: AsyncClient):
    resp = await client.get("/api/providers", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "name" in data[0]


@pytest.mark.asyncio
async def test_patch_provider_toggle_enabled(client: AsyncClient):
    resp = await client.patch(
        "/api/providers/groq",
        headers=AUTH_HEADERS,
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["provider"]["enabled"] is False


# ──────────── config ────────────

@pytest.mark.asyncio
async def test_get_config(client: AsyncClient):
    resp = await client.get("/api/config", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "default_strategy" in data
    assert "enable_fallback" in data


@pytest.mark.asyncio
async def test_set_strategy_unknown_returns_400(client: AsyncClient):
    resp = await client.put(
        "/api/config/strategy",
        headers=AUTH_HEADERS,
        json={"default_strategy": "nonexistent_xyz"},
    )
    assert resp.status_code == 400


# ──────────── strategies ────────────

@pytest.mark.asyncio
async def test_strategy_crud_with_definition(client: AsyncClient):
    """Happy path on the new DSL shape: create with `definition`,
    update it, then delete."""
    create_def = {
        "require": [],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
        ],
    }
    resp = await client.post(
        "/api/strategies",
        headers=AUTH_HEADERS,
        json={"name": "test_e2e", "definition": create_def, "description": "e2e test"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test_e2e"
    assert body["definition"] == create_def

    update_def = {
        "require": [],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
            {"field": "tags", "op": "contains", "value": "cheap", "weight": 3},
        ],
    }
    resp = await client.patch(
        "/api/strategies/test_e2e",
        headers=AUTH_HEADERS,
        json={"name": "test_e2e", "definition": update_def, "description": "updated"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"] == update_def

    resp = await client.delete("/api/strategies/test_e2e", headers=AUTH_HEADERS)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_strategy_legacy_tags_bridge(client: AsyncClient):
    """Bridge: a payload still using the old `tags` field is accepted and
    converted to a `prefer.contains` per tag with weight 5. Lossless
    rewrite for clients that haven't migrated yet. Removed in commit 4."""
    resp = await client.post(
        "/api/strategies",
        headers=AUTH_HEADERS,
        json={"name": "legacy", "tags": ["fast", "cheap"], "description": "old shape"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["definition"] == {
        "require": [],
        "prefer": [
            {"field": "tags", "op": "contains", "value": "fast", "weight": 5},
            {"field": "tags", "op": "contains", "value": "cheap", "weight": 5},
        ],
    }
    await client.delete("/api/strategies/legacy", headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_strategy_create_rejects_invalid_definition(client: AsyncClient):
    """A definition that fails the DSL parser should be rejected with 422
    and a human-readable message — not stored as garbage."""
    resp = await client.post(
        "/api/strategies",
        headers=AUTH_HEADERS,
        json={
            "name": "bad",
            "definition": {
                "require": [{"field": "creativity", "op": "==", "value": "high"}],
            },
        },
    )
    assert resp.status_code == 422
    assert "unknown field" in resp.json()["detail"]


# ──────────── clients ────────────

@pytest.mark.asyncio
async def test_client_crud(client: AsyncClient):
    resp = await client.post(
        "/api/clients",
        headers=AUTH_HEADERS,
        json={"name": "e2e-client", "rpm_limit": 10},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key"].startswith("fai_")
    key_hash = data["key_hash"]

    resp = await client.get("/api/clients", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert any(c["key_hash"] == key_hash for c in resp.json())

    resp = await client.delete(f"/api/clients/{key_hash}", headers=AUTH_HEADERS)
    assert resp.status_code == 200


# ──────────── analytics ────────────

@pytest.mark.asyncio
async def test_analytics_returns_empty(client: AsyncClient):
    resp = await client.get(
        "/api/analytics?window_seconds=3600&bucket_count=12",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 0


@pytest.mark.asyncio
async def test_analytics_bad_window(client: AsyncClient):
    resp = await client.get(
        "/api/analytics?window_seconds=10",
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400


# ──────────── metrics ────────────

@pytest.mark.asyncio
async def test_metrics_endpoint(client: AsyncClient):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert b"freeai_http_requests_total" in resp.content

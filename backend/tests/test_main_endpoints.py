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


@pytest_asyncio.fixture
async def auth_headers(seeded_session):
    """JWT headers for the seeded admin user. Endpoints behind require_admin_user
    only accept JWT, not the legacy admin token."""
    from app.auth import create_access_token
    from app.repositories.user_repo import UserRepository
    user = await UserRepository(seeded_session).find_by_username("testadmin")
    token = create_access_token(user.id, user.username, user.role)
    return {"Authorization": f"Bearer {token}"}


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
    assert data == {"status": "ok"}


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
async def test_providers_list_with_admin_token(client: AsyncClient, auth_headers):
    resp = await client.get("/api/providers", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "name" in data[0]


@pytest.mark.asyncio
async def test_patch_provider_toggle_enabled(client: AsyncClient, auth_headers):
    resp = await client.patch(
        "/api/providers/groq",
        headers=auth_headers,
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["provider"]["enabled"] is False


# ──────────── config ────────────

@pytest.mark.asyncio
async def test_get_config(client: AsyncClient, auth_headers):
    resp = await client.get("/api/config", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "default_strategy" in data
    assert "enable_fallback" in data


@pytest.mark.asyncio
async def test_set_strategy_unknown_returns_400(client: AsyncClient, auth_headers):
    resp = await client.put(
        "/api/config/strategy",
        headers=auth_headers,
        json={"default_strategy": "nonexistent_xyz"},
    )
    assert resp.status_code == 400


# ──────────── strategies ────────────

@pytest.mark.asyncio
async def test_strategy_crud_with_definition(client: AsyncClient, auth_headers):
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
        headers=auth_headers,
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
        headers=auth_headers,
        json={"name": "test_e2e", "definition": update_def, "description": "updated"},
    )
    assert resp.status_code == 200
    assert resp.json()["definition"] == update_def

    resp = await client.delete("/api/strategies/test_e2e", headers=auth_headers)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_strategy_create_rejects_invalid_definition(client: AsyncClient, auth_headers):
    """A definition that fails the DSL parser should be rejected with 422
    and a human-readable message — not stored as garbage."""
    resp = await client.post(
        "/api/strategies",
        headers=auth_headers,
        json={
            "name": "bad",
            "definition": {
                "require": [{"field": "creativity", "op": "==", "value": "high"}],
            },
        },
    )
    assert resp.status_code == 422
    assert "unknown field" in resp.json()["detail"]


# ──────────── tags vocabulary ────────────

@pytest.mark.asyncio
async def test_list_tags_returns_seeded_vocabulary(client: AsyncClient, auth_headers):
    """The default seeded providers carry tags like fast/cheap/coding/...
    GET /api/tags should surface every distinct one with its providers."""
    resp = await client.get("/api/tags", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    # Index by tag for easy assertion.
    by_tag = {t["tag"]: set(t["providers"]) for t in data}
    # `fast` is on groq + mistral + cohere in DEFAULT_PROVIDERS.
    assert "fast" in by_tag
    assert "groq" in by_tag["fast"]
    # `vision` is only on gemini.
    assert by_tag.get("vision") == {"gemini"}


@pytest.mark.asyncio
async def test_list_tags_requires_admin(client: AsyncClient):
    resp = await client.get("/api/tags")
    assert resp.status_code == 401


# ──────────── strategy preview ────────────

@pytest.mark.asyncio
async def test_preview_with_empty_definition_lists_all_eligible(client: AsyncClient, auth_headers):
    """An empty definition is the baseline-only strategy. Without any
    provider api keys configured the preview returns no candidates and
    surfaces a clear warning."""
    resp = await client.post(
        "/api/strategies/preview",
        headers=auth_headers,
        json={"definition": None},
    )
    assert resp.status_code == 200
    body = resp.json()
    # In the test fixture there are no api keys yet, so nothing is eligible.
    assert body["candidates"] == []
    assert any("no providers configured" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_preview_with_eligible_providers_ranks_them(
    client: AsyncClient, seeded_session, auth_headers
):
    """With at least one provider that has a key, the preview ranks it.
    Use a require clause that matches the seeded `gemini` (vision) and
    confirm groq is excluded."""
    # Give two providers fake keys so they show up as eligible.
    repo = ConfigRepository(seeded_session)
    groq = await repo.get_provider("groq")
    groq.api_key = "fake_key_for_test"
    await repo.upsert_provider(groq)
    gemini = await repo.get_provider("gemini")
    gemini.api_key = "fake_key_for_test"
    await repo.upsert_provider(gemini)
    await seeded_session.commit()

    resp = await client.post(
        "/api/strategies/preview",
        headers=auth_headers,
        json={"definition": {
            "require": [{"field": "tags", "op": "contains", "value": "vision"}],
            "prefer": [],
        }},
    )
    assert resp.status_code == 200
    body = resp.json()
    candidate_names = [c["name"] for c in body["candidates"]]
    # Only gemini has the vision tag.
    assert candidate_names == ["gemini"]
    assert "groq" in body["excluded"]


@pytest.mark.asyncio
async def test_preview_warns_about_unknown_tags(
    client: AsyncClient, seeded_session, auth_headers
):
    """A prefer clause referencing a tag no provider has should produce
    a warning, but the strategy is still previewable (it just won't fire
    that clause)."""
    repo = ConfigRepository(seeded_session)
    groq = await repo.get_provider("groq")
    groq.api_key = "fake_key_for_test"
    await repo.upsert_provider(groq)
    await seeded_session.commit()

    resp = await client.post(
        "/api/strategies/preview",
        headers=auth_headers,
        json={"definition": {
            "require": [],
            "prefer": [{
                "field": "tags", "op": "contains", "value": "unicornio", "weight": 5
            }],
        }},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert any("'unicornio'" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_preview_rejects_invalid_definition(client: AsyncClient, auth_headers):
    resp = await client.post(
        "/api/strategies/preview",
        headers=auth_headers,
        json={"definition": {
            "require": [{"field": "creativity", "op": "==", "value": "high"}],
        }},
    )
    assert resp.status_code == 422
    assert "unknown field" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_preview_requires_admin(client: AsyncClient):
    resp = await client.post(
        "/api/strategies/preview",
        json={"definition": None},
    )
    assert resp.status_code == 401


# ──────────── clients ────────────

@pytest.mark.asyncio
async def test_client_crud(client: AsyncClient, auth_headers):
    resp = await client.post(
        "/api/clients",
        headers=auth_headers,
        json={"name": "e2e-client", "rpm_limit": 10},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["api_key"].startswith("fai_")
    key_hash = data["key_hash"]

    resp = await client.get("/api/clients", headers=auth_headers)
    assert resp.status_code == 200
    assert any(c["key_hash"] == key_hash for c in resp.json())

    resp = await client.delete(f"/api/clients/{key_hash}", headers=auth_headers)
    assert resp.status_code == 200


# ──────────── analytics ────────────

@pytest.mark.asyncio
async def test_analytics_returns_empty(client: AsyncClient, auth_headers):
    resp = await client.get(
        "/api/analytics?window_seconds=3600&bucket_count=12",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 0


@pytest.mark.asyncio
async def test_analytics_bad_window(client: AsyncClient, auth_headers):
    resp = await client.get(
        "/api/analytics?window_seconds=10",
        headers=auth_headers,
    )
    assert resp.status_code == 400


# ──────────── metrics ────────────

@pytest.mark.asyncio
async def test_metrics_endpoint(client: AsyncClient):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert b"freeai_http_requests_total" in resp.content

"""First-run UI setup: public status + POST initial (admin hash + provider keys)."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def no_admin_file(monkeypatch, tmp_path, database_url):
    monkeypatch.delenv("FREEAI_ADMIN_TOKEN", raising=False)
    from app import settings as st

    st.get_settings.cache_clear()
    bogus = tmp_path / "absent_admin_token_path"
    custom = st.Settings().model_copy(
        update={
            "admin_token": None,
            "admin_token_path": bogus,
        }
    )
    def _fake():
        return custom

    for target in (
        "app.settings.get_settings",
        "app.security.get_settings",
        "app.main.get_settings",
        "app.db.engine.get_settings",
        "app.logging_config.get_settings",
    ):
        monkeypatch.setattr(target, _fake)


def _patch_fresh_settings(monkeypatch):
    """Bypass @lru_cache so each call reads current os.environ (stable across tests)."""
    from app import settings as st

    def fresh():
        return st.Settings()

    for target in (
        "app.settings.get_settings",
        "app.security.get_settings",
        "app.main.get_settings",
        "app.db.engine.get_settings",
        "app.logging_config.get_settings",
    ):
        monkeypatch.setattr(target, fresh)


def test_setup_initial_http_flow(database_url, no_admin_file, session):
    from app.main import app

    tok = "adm_integration_setup_token_12"
    with TestClient(app, raise_server_exceptions=False) as client:
        st = client.get("/api/setup/status")
        assert st.json()["needs_initial_setup"] is True
        r = client.post(
            "/api/setup/initial",
            json={
                "admin_token": tok,
                "admin_token_confirm": tok,
                "provider_keys": {"groq": "sk-test-freeai-setup-99"},
            },
        )
        assert r.status_code == 201
        st2 = client.get("/api/setup/status")
        assert st2.json()["needs_initial_setup"] is False
        bad = client.get("/api/providers")
        assert bad.status_code == 401
        ok = client.get("/api/providers", headers={"X-Admin-Token": tok})
        assert ok.status_code == 200
        groq = next(p for p in ok.json() if p["name"] == "groq")
        assert groq["has_key"] is True
        dup = client.post(
            "/api/setup/initial",
            json={
                "admin_token": tok,
                "admin_token_confirm": tok,
                "provider_keys": {},
            },
        )
        assert dup.status_code == 403


def test_setup_status_false_when_env_admin(database_url, monkeypatch):
    monkeypatch.setenv("FREEAI_ADMIN_TOKEN", "adm_only_from_env_test")
    _patch_fresh_settings(monkeypatch)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["needs_initial_setup"] is False
    assert "groq" in body["provider_names"]

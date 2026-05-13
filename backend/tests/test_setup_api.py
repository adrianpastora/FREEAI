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

    # Build a fresh Settings on every call so that monkeypatch.setenv() calls
    # made by the test (after this fixture ran) are reflected in subsequent
    # requests. Snapshotting once at fixture time would freeze env vars.
    def _fresh():
        s = st.Settings()
        return s.model_copy(update={"admin_token": None, "admin_token_path": bogus})

    for target in (
        "app.settings.get_settings",
        "app.security.get_settings",
        "app.main.get_settings",
        "app.db.engine.get_settings",
        "app.logging_config.get_settings",
    ):
        monkeypatch.setattr(target, _fresh)


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


def test_setup_initial_http_flow(database_url, no_admin_file, session, monkeypatch):
    """Legacy admin-token wizard (opt-in via FREEAI_LEGACY_INITIAL_SETUP)."""
    monkeypatch.setenv("FREEAI_LEGACY_INITIAL_SETUP", "true")
    from app.main import app
    from app.bootstrap import read_bootstrap_token

    tok = "adm_integration_setup_token_12"
    with TestClient(app, raise_server_exceptions=False) as client:
        st = client.get("/api/setup/status")
        assert st.json()["needs_initial_setup"] is True

        # Without the bootstrap token the setup wizard refuses.
        refused = client.post(
            "/api/setup/initial",
            json={
                "admin_token": tok,
                "admin_token_confirm": tok,
                "provider_keys": {"groq": "sk-test-freeai-setup-99"},
            },
        )
        assert refused.status_code == 401

        bootstrap = read_bootstrap_token()
        assert bootstrap, "lifespan should have generated a bootstrap token"

        r = client.post(
            "/api/setup/initial",
            headers={"X-Bootstrap-Token": bootstrap},
            json={
                "admin_token": tok,
                "admin_token_confirm": tok,
                "provider_keys": {"groq": "sk-test-freeai-setup-99"},
            },
        )
        assert r.status_code == 201
        st2 = client.get("/api/setup/status")
        assert st2.json()["needs_initial_setup"] is False
        # /api/providers needs JWT (require_admin_user); the legacy admin
        # token still works on require_admin endpoints like /api/config/fallback.
        bad = client.put("/api/config/fallback", json={"enable_fallback": True})
        assert bad.status_code == 401
        ok = client.put(
            "/api/config/fallback",
            headers={"X-Admin-Token": tok},
            json={"enable_fallback": True},
        )
        assert ok.status_code == 200
        dup = client.post(
            "/api/setup/initial",
            json={
                "admin_token": tok,
                "admin_token_confirm": tok,
                "provider_keys": {},
            },
        )
        assert dup.status_code == 403


def test_setup_first_admin_without_pending_master(database_url, no_admin_file, monkeypatch):
    """JWT first admin in one step when master key already comes from env (tests)."""
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.bootstrap import read_bootstrap_token
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        b = read_bootstrap_token()
        assert b
        r = client.post(
            "/api/setup/first-admin",
            headers={"X-Bootstrap-Token": b},
            json={
                "username": "firstadm",
                "password": "longpassword1",
                "password_confirm": "longpassword1",
            },
        )
        assert r.status_code == 201, r.text
        st = client.get("/api/auth/status")
        assert st.json()["status"] == "ready"


def test_setup_status_includes_master_key_flag(database_url, no_admin_file, monkeypatch):
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert "needs_master_key_confirm" in body
    assert body["needs_master_key_confirm"] in (True, False)


def test_setup_status_false_when_env_admin(database_url, monkeypatch):
    monkeypatch.setenv("FREEAI_ADMIN_TOKEN", "adm_only_from_env_test")
    _patch_fresh_settings(monkeypatch)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["needs_initial_setup"] is False
    assert body.get("needs_master_key_confirm") is False
    assert "groq" in body["provider_names"]


# ─────────────── default vs paranoid mode ───────────────


def test_status_paranoid_flag_default_off(database_url, no_admin_file, monkeypatch):
    """Default mode must report paranoid_mode=false so the frontend hides the
    bootstrap-token / master-key fields by default."""
    monkeypatch.delenv("FREEAI_REQUIRE_BOOTSTRAP_HEADER", raising=False)
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/setup/status")
    assert r.status_code == 200
    assert r.json()["paranoid_mode"] is False


def test_status_paranoid_flag_on_when_env_set(database_url, no_admin_file, monkeypatch):
    monkeypatch.setenv("FREEAI_REQUIRE_BOOTSTRAP_HEADER", "true")
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/setup/status")
    assert r.status_code == 200
    assert r.json()["paranoid_mode"] is True


def test_bootstrap_token_endpoint_refuses_non_loopback_peer(
    database_url, no_admin_file, monkeypatch,
):
    """Anything that isn't 127.0.0.1 / ::1 / localhost is treated as a remote
    peer. TestClient defaults to ``client.host = "testclient"`` which is
    exactly such a remote peer, so we get to verify the deny path here."""
    monkeypatch.delenv("FREEAI_REQUIRE_BOOTSTRAP_HEADER", raising=False)
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/setup/bootstrap-token")
    assert r.status_code == 403
    assert "loopback" in r.json()["detail"].lower()


def test_bootstrap_token_endpoint_returns_token_to_loopback(
    database_url, no_admin_file, monkeypatch,
):
    """When the peer really is loopback, the endpoint hands the token back
    so the frontend can use it without bothering the user."""
    monkeypatch.delenv("FREEAI_REQUIRE_BOOTSTRAP_HEADER", raising=False)
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.main import app
    from app.bootstrap import read_bootstrap_token

    expected = None

    # TestClient lets us pass a custom client (host, port) tuple so request.client.host
    # ends up as we want. This mirrors what a real browser on the same machine sees.
    with TestClient(
        app, raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
    ) as client:
        expected = read_bootstrap_token()
        assert expected
        r = client.get("/api/setup/bootstrap-token")
    assert r.status_code == 200, r.text
    assert r.json()["token"] == expected


def test_bootstrap_token_endpoint_refused_in_paranoid_mode(
    database_url, no_admin_file, monkeypatch,
):
    monkeypatch.setenv("FREEAI_REQUIRE_BOOTSTRAP_HEADER", "true")
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/setup/bootstrap-token")
    assert r.status_code == 403
    assert "paranoid" in r.json()["detail"].lower()


def test_first_admin_default_mode_omits_master_key_field(
    database_url, no_admin_file, monkeypatch,
):
    """Default mode: a body without ``master_key`` is accepted because the
    server auto-confirmed the pending key during lifespan."""
    monkeypatch.delenv("FREEAI_REQUIRE_BOOTSTRAP_HEADER", raising=False)
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.bootstrap import read_bootstrap_token
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        b = read_bootstrap_token()
        assert b
        r = client.post(
            "/api/setup/first-admin",
            headers={"X-Bootstrap-Token": b},
            json={
                "username": "defaultadm",
                "password": "longpassword1",
                "password_confirm": "longpassword1",
            },
        )
    assert r.status_code == 201, r.text


def test_first_admin_rejects_request_without_bootstrap_token(
    database_url, no_admin_file, monkeypatch,
):
    """Both modes: the X-Bootstrap-Token header is mandatory. Default mode
    just lets the frontend fetch it for the user — it never disappears
    from the protocol."""
    monkeypatch.delenv("FREEAI_REQUIRE_BOOTSTRAP_HEADER", raising=False)
    monkeypatch.delenv("FREEAI_LEGACY_INITIAL_SETUP", raising=False)
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(
            "/api/setup/first-admin",
            json={
                "username": "should_fail",
                "password": "longpassword1",
                "password_confirm": "longpassword1",
            },
        )
    assert r.status_code == 401
    assert "bootstrap" in r.json()["detail"].lower()

#!/usr/bin/env python3
"""Diagnose the state of a FreeAI install --answer "why am I seeing this?"

When the first-run modal asks for a bootstrap token or a master key paste,
it's because *something* in the current environment is putting the server
into a non-default mode. The reasons are scattered across env vars, files
under ``backend/data/``, and rows in Postgres. This script collects all of
them in one place and tells you, in plain English, which mode you're in
and why.

Run from the repo root:

    python scripts/doctor.py

The script is read-only --it never mutates files or the database. Safe to
run any time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "backend" / "data"
ENV_FILE = REPO_ROOT / ".env"


# ANSI styling --degrade gracefully when stdout isn't a TTY.
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _USE_COLOR else text


# Using ASCII glyphs instead of unicode (✓ • ✗) because cp1252 on Windows
# can't encode them when stdout is the legacy console codepage.
def _ok(text: str) -> str:    return _c("32", "[ok]   " + text)
def _warn(text: str) -> str:  return _c("33", "[warn] " + text)
def _bad(text: str) -> str:   return _c("31", "[err]  " + text)
def _info(text: str) -> str:  return _c("36", "[info] " + text)


def _dotenv_value(key: str) -> str | None:
    """Read a key from .env without importing pydantic-settings."""
    if not ENV_FILE.exists():
        return None
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _effective(key: str) -> tuple[str | None, str]:
    """Return (value, source) for an env var, checking process env first then .env."""
    if (v := os.environ.get(key)) is not None:
        return v, "process env"
    if (v := _dotenv_value(key)) is not None:
        return v, ".env"
    return None, ""


def main() -> int:
    print(_c("1", "FreeAI doctor --state of this install"))
    print()
    print(_info(f"repo root:  {REPO_ROOT}"))
    print(_info(f"data dir:   {DATA_DIR}"))
    print(_info(f".env file:  {ENV_FILE} ({'present' if ENV_FILE.exists() else 'absent'})"))
    print()

    # -- secrets on disk ----------------------------------------------------
    print(_c("1", "Files under backend/data/"))
    items = [
        (".master_key",         "Fernet key that decrypts every stored provider key. Back this up."),
        (".master_key.pending", "Pending key --server is waiting for an operator to confirm it via the UI."),
        (".bootstrap_token",    "One-time token used by the first-admin modal. Auto-deleted after first use."),
        (".jwt_secret",         "HMAC secret for the session JWT. Regenerated if missing (existing sessions die)."),
        ("admin_token",         "Legacy admin token (pre-multi-user installs). Presence forces legacy mode."),
        ("config.json",         "Legacy JSON config from before Postgres. Read-only fossil --safe to ignore."),
    ]
    for name, purpose in items:
        p = DATA_DIR / name
        if p.exists():
            print(_ok(f"{name:<24} present --{purpose}"))
        else:
            print(_c("90", f"  {name:<24} absent  --{purpose}"))
    print()

    # -- relevant env vars --------------------------------------------------
    print(_c("1", "Environment variables that change behaviour"))
    var_specs = [
        ("FREEAI_MASTER_KEY",
         "If set, replaces the .master_key file entirely. Useful in CI / containers."),
        ("FREEAI_ADMIN_TOKEN",
         "Pre-shared admin token. Presence puts the server in legacy admin-token mode "
         "and HIDES the friendly username/password create-admin modal."),
        ("FREEAI_REQUIRE_BOOTSTRAP_HEADER",
         "When true, paranoid mode is on: the bootstrap token and master key are NOT "
         "auto-handed off; the operator must paste both into the modal."),
        ("FREEAI_LEGACY_INITIAL_SETUP",
         "Re-enables the old admin-token wizard. Only relevant for migrations from <0.5."),
        ("FREEAI_AUTO_MIGRATE",
         "Run alembic migrations at startup. Off by default in dev, on in Docker."),
        ("FREEAI_DATABASE_URL",
         "Where Postgres lives. Defaults to the docker-compose service URL."),
    ]
    for var, purpose in var_specs:
        val, source = _effective(var)
        if val is None:
            print(_c("90", f"  {var:<33}  unset   --{purpose}"))
        elif val == "":
            print(_ok(f"{var:<33}  empty (from {source})"))
        else:
            shown = val if len(val) <= 18 else f"{val[:6]}...{val[-4:]} ({len(val)} chars)"
            print(_warn(f"{var:<33}  set to {shown} (from {source})"))
            print(_c("90", f"  {'':<35}  +-- {purpose}"))
    print()

    # -- verdict ------------------------------------------------------------
    print(_c("1", "Mode this install is in"))

    legacy_token, _ = _effective("FREEAI_ADMIN_TOKEN")
    paranoid, _ = _effective("FREEAI_REQUIRE_BOOTSTRAP_HEADER")
    legacy_wizard, _ = _effective("FREEAI_LEGACY_INITIAL_SETUP")
    has_admin_token_file = (DATA_DIR / "admin_token").exists()
    has_pending_master = (DATA_DIR / ".master_key.pending").exists()
    paranoid_on = (paranoid or "").lower() in ("1", "true", "yes", "on")
    legacy_wizard_on = (legacy_wizard or "").lower() in ("1", "true", "yes", "on")

    if paranoid_on:
        print(_warn("Paranoid mode (FREEAI_REQUIRE_BOOTSTRAP_HEADER is truthy)."))
        print("    The create-admin modal will ask for the bootstrap token AND the master")
        print("    key, printed in banners on server startup. Look at the uvicorn logs.")
    elif legacy_token or has_admin_token_file:
        print(_warn("Legacy admin-token mode --_needs_initial_setup() short-circuits."))
        print("    Source: " + ("FREEAI_ADMIN_TOKEN env/.env" if legacy_token else f"the file {DATA_DIR / 'admin_token'}"))
        print("    The friendly username+password create-admin modal will NOT appear.")
        print("    To use the modern modal, remove the env var / .env line / file and restart.")
    elif legacy_wizard_on:
        print(_warn("Legacy wizard mode (FREEAI_LEGACY_INITIAL_SETUP is truthy)."))
        print("    The full first-run wizard with admin-token + provider keys will run.")
    elif has_pending_master:
        print(_warn(".master_key.pending exists --the server is waiting for confirmation."))
        print("    In default mode the lifespan should auto-promote it. Restart and check logs.")
    else:
        print(_ok("Default mode."))
        print("    The create-admin modal will show only username + password fields. The")
        print("    bootstrap token is fetched automatically via loopback and the master key")
        print("    is auto-confirmed on the next startup.")

    print()
    print(_c("90", "If the running UI doesn't match this verdict, hard-reload the page (Ctrl+F5)"))
    print(_c("90", "to discard any stale HTML/JS cached from a previous mode."))
    return 0


if __name__ == "__main__":
    sys.exit(main())

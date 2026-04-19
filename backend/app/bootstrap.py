"""Bootstrap token — prevents drive-by takeover of the setup wizard.

When a fresh instance starts with no admin and no users, we generate a random
one-use token and print it to stdout. The first call to /api/setup/initial or
/api/auth/register must present it via the X-Bootstrap-Token header. Once
consumed, the token file is deleted.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from .settings import get_settings

log = logging.getLogger("freeai.bootstrap")


def ensure_bootstrap_token(*, needed: bool) -> Optional[str]:
    """Generate the bootstrap token if needed and not already on disk.

    Returns the token on generation so the caller can log it, or None if it
    already existed or is no longer needed. Safe to call repeatedly.
    """
    path = get_settings().bootstrap_token_path
    if not needed:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return None

    if path.exists():
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return token


def read_bootstrap_token() -> Optional[str]:
    path = get_settings().bootstrap_token_path
    if not path.exists():
        return None
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def consume_bootstrap_token() -> None:
    path = get_settings().bootstrap_token_path
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("could not delete bootstrap token file: %s", e)


def verify_bootstrap_token(provided: Optional[str]) -> bool:
    expected = read_bootstrap_token()
    if not expected:
        return False
    if not provided:
        return False
    return secrets.compare_digest(expected, provided)

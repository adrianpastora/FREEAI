"""JWT authentication for multi-user support.

Provides access tokens (short-lived, 15 min) and refresh tokens (7 days,
hash stored in DB). Password hashing reuses the bcrypt(SHA256(plain))
pattern from crypto.py for consistency.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import bcrypt
import jwt

from .settings import get_settings

_ALGORITHM = "HS256"

log = logging.getLogger("freeai.auth")


# ── password helpers (same pattern as crypto.hash_admin_token) ──


def hash_password(plain: str) -> str:
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    try:
        return bcrypt.checkpw(digest, stored_hash.encode("ascii"))
    except Exception:
        return False


# ── JWT helpers ──


def _get_jwt_secret() -> str:
    settings = get_settings()
    if settings.jwt_secret:
        return settings.jwt_secret

    path = settings.jwt_secret_path
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return secret

    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(64)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    log.warning(
        "Generated new JWT secret at %s — back this file up. "
        "Set FREEAI_JWT_SECRET in production instead.",
        path,
    )
    return secret


def create_access_token(
    user_id: int,
    username: str,
    role: str,
    expires_seconds: Optional[int] = None,
) -> str:
    settings = get_settings()
    if expires_seconds is None:
        expires_seconds = settings.jwt_access_expire_minutes * 60
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": int(time.time()) + expires_seconds,
        "iat": int(time.time()),
        "type": "access",
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=_ALGORITHM)


def create_refresh_token() -> tuple[str, str]:
    """Return (raw_token, token_hash). Raw is given to the client; hash is stored."""
    raw = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return raw, token_hash


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate an access token. Returns payload or None."""
    try:
        payload = jwt.decode(token, _get_jwt_secret(), algorithms=[_ALGORITHM])
        if payload.get("type") != "access":
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ── CurrentUser dataclass ──


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

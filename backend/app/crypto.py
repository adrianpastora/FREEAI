"""At-rest encryption for provider API keys.

Uses Fernet (AES-128-CBC + HMAC-SHA256). The master key comes from:

  1. FREEAI_MASTER_KEY env var (preferred — operator controls the secret)
  2. data/.master_key file (auto-generated on first run, chmod 600)

Stored values are prefixed with "enc::" so we can distinguish encrypted from
plaintext values during migration. Plain values are silently re-encrypted on
the next save.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("freeai.crypto")

ENC_PREFIX = "enc::"
MASTER_KEY_PATH = Path(
    os.environ.get(
        "FREEAI_MASTER_KEY_PATH",
        Path(__file__).parent.parent / "data" / ".master_key",
    )
)


def _load_master_key() -> bytes:
    env = os.environ.get("FREEAI_MASTER_KEY")
    if env:
        # Accept either a Fernet key directly or any string we'll derive from.
        try:
            Fernet(env.encode())
            return env.encode()
        except Exception:
            # Derive a Fernet key from arbitrary input via SHA-256
            import hashlib
            digest = hashlib.sha256(env.encode("utf-8")).digest()
            return base64.urlsafe_b64encode(digest)

    if MASTER_KEY_PATH.exists():
        return MASTER_KEY_PATH.read_text(encoding="utf-8").strip().encode()

    MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    MASTER_KEY_PATH.write_bytes(key)
    try:
        os.chmod(MASTER_KEY_PATH, 0o600)
    except Exception:
        pass
    log.warning(
        "Generated new master key at %s — back this file up. "
        "Set FREEAI_MASTER_KEY in production instead.",
        MASTER_KEY_PATH,
    )
    return key


_fernet = Fernet(_load_master_key())


def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return value
    if value.startswith(ENC_PREFIX):
        return value  # already encrypted
    token = _fernet.encrypt(value.encode("utf-8")).decode("utf-8")
    return ENC_PREFIX + token


def decrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return value
    if not value.startswith(ENC_PREFIX):
        # legacy plaintext — caller is responsible for re-encrypting on save
        return value
    token = value[len(ENC_PREFIX):]
    try:
        return _fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        log.error("could not decrypt value — wrong master key?")
        return None


def hash_admin_token(plain: str) -> str:
    """bcrypt(SHA256(utf8(plain))) so tokens of any length work."""
    import bcrypt
    import hashlib

    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_admin_token_hash(plain: str, stored_hash: str) -> bool:
    import bcrypt
    import hashlib

    if not stored_hash:
        return False
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    try:
        return bcrypt.checkpw(digest, stored_hash.encode("ascii"))
    except Exception:
        return False

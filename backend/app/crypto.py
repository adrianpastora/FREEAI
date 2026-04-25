"""At-rest encryption for provider API keys.

Uses Fernet (AES-128-CBC + HMAC-SHA256). The master key comes from:

  1. ``FREEAI_MASTER_KEY`` env var (preferred — operator controls the secret)
  2. ``data/.master_key`` file after first-run confirmation

On a **fresh** install with neither env nor file, the server writes
``data/.master_key.pending`` and prints the key once to stdout. The operator
pastes it in the web UI; we then promote it to ``.master_key`` and optionally
append ``FREEAI_MASTER_KEY=…`` to a local ``.env`` (never committed).

Stored values are prefixed with "enc::" so we can distinguish encrypted from
plaintext values during migration. Plain values are silently re-encrypted on
the next save.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .logging_config import get_logger

log = get_logger("freeai.crypto")

ENC_PREFIX = "enc::"
MASTER_KEY_PATH = Path(
    os.environ.get(
        "FREEAI_MASTER_KEY_PATH",
        Path(__file__).parent.parent / "data" / ".master_key",
    )
)
MASTER_KEY_PENDING_PATH = MASTER_KEY_PATH.parent / ".master_key.pending"

_fernet_instance: Optional[Fernet] = None


class MasterKeyNotReadyError(RuntimeError):
    """Encrypt/decrypt called before the operator confirmed the pending master key."""


def _chmod600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _strict_fernet_key_bytes(raw: str) -> bytes:
    try:
        Fernet(raw.encode())
        return raw.encode()
    except (ValueError, TypeError):
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)


def _bytes_from_master_file(path: Path) -> Optional[bytes]:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip().encode()
    if not raw:
        return None
    try:
        Fernet(raw)
        return raw
    except (ValueError, TypeError):
        return _strict_fernet_key_bytes(raw.decode("utf-8"))


def _master_key_material_from_env_or_file() -> Optional[bytes]:
    env = os.environ.get("FREEAI_MASTER_KEY")
    if env:
        return _strict_fernet_key_bytes(env)
    return _bytes_from_master_file(MASTER_KEY_PATH)


def clear_fernet_cache() -> None:
    global _fernet_instance
    _fernet_instance = None


def get_fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    material = _master_key_material_from_env_or_file()
    if material is None:
        raise MasterKeyNotReadyError(
            "master encryption key is not active — confirm the key printed at "
            "startup via the web UI (POST /api/setup/confirm-master-key)."
        )
    _fernet_instance = Fernet(material)
    return _fernet_instance


def is_master_key_ready() -> bool:
    return _master_key_material_from_env_or_file() is not None


def master_key_confirmation_required() -> bool:
    """True when a pending key exists and no active env/file material yet."""
    cleanup_pending_if_master_active()
    if not MASTER_KEY_PENDING_PATH.exists():
        return False
    return _master_key_material_from_env_or_file() is None


def clear_stale_pending_when_env_master() -> None:
    if os.environ.get("FREEAI_MASTER_KEY") and MASTER_KEY_PENDING_PATH.exists():
        try:
            MASTER_KEY_PENDING_PATH.unlink()
        except OSError:
            pass


def cleanup_pending_if_master_active() -> None:
    if _master_key_material_from_env_or_file() is not None and MASTER_KEY_PENDING_PATH.exists():
        try:
            MASTER_KEY_PENDING_PATH.unlink()
        except OSError:
            pass


def read_pending_master_key_plaintext() -> Optional[str]:
    if not MASTER_KEY_PENDING_PATH.exists():
        return None
    try:
        s = MASTER_KEY_PENDING_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return s or None


def ensure_pending_master_key() -> Optional[str]:
    """Create ``.master_key.pending`` on first cold start; return key for stdout.

    Returns None if env or ``.master_key`` already exists, or pending already
    exists (operator must use logs or delete the pending file to regenerate).
    """
    clear_stale_pending_when_env_master()
    cleanup_pending_if_master_active()
    if os.environ.get("FREEAI_MASTER_KEY"):
        return None
    if MASTER_KEY_PATH.exists():
        return None
    if MASTER_KEY_PENDING_PATH.exists():
        return None

    MASTER_KEY_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    MASTER_KEY_PENDING_PATH.write_bytes(key)
    _chmod600(MASTER_KEY_PENDING_PATH)
    log.warning(
        "master_key_pending_created",
        path=str(MASTER_KEY_PENDING_PATH),
        hint="paste into UI to activate; key also printed to stdout",
    )
    return key.decode("ascii")


def _pending_matches(pasted: str) -> bool:
    expected = read_pending_master_key_plaintext()
    if not expected or not pasted:
        return False
    return secrets.compare_digest(expected.strip(), pasted.strip())


def confirm_pending_master_key(pasted: str) -> bool:
    """If ``pasted`` matches pending, promote to ``.master_key`` and reload Fernet.

    Returns False on mismatch or if there is nothing pending.
    """
    if not _pending_matches(pasted):
        return False
    try:
        raw = read_pending_master_key_plaintext()
        if not raw:
            return False
        key_bytes = raw.encode("utf-8")
        Fernet(key_bytes)  # validate
        MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MASTER_KEY_PATH.write_bytes(key_bytes)
        _chmod600(MASTER_KEY_PATH)
        MASTER_KEY_PENDING_PATH.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError) as e:
        log.error("master_key_promote_failed", error=str(e))
        return False
    clear_fernet_cache()
    try_append_master_key_to_dotenv(raw)
    log.info("master_key_activated", path=str(MASTER_KEY_PATH))
    return True


def try_append_master_key_to_dotenv(master_key: str) -> None:
    """Write ``FREEAI_MASTER_KEY`` into a local ``.env`` when possible.

    If ``.env`` exists and is writable, appends the line when not already set.
    If it does not exist but ``docker-compose.yml`` sits in the same directory,
    creates a minimal ``.env`` (operator should add ``POSTGRES_PASSWORD``, etc.).

    Tries the process working directory and the repository root (parent of
    ``backend/``). Never raises for I/O failure.
    """
    line_key = "FREEAI_MASTER_KEY="
    roots: list[Path] = [Path.cwd()]
    try:
        repo = Path(__file__).resolve().parent.parent.parent
        if repo not in roots:
            roots.append(repo)
    except OSError:
        pass
    for root in roots:
        env_path = root / ".env"
        compose = root / "docker-compose.yml"
        try:
            if env_path.is_file():
                text = env_path.read_text(encoding="utf-8", errors="replace")
                if any(
                    ln.strip().startswith(line_key) and not ln.strip().startswith(f"{line_key}#")
                    for ln in text.splitlines()
                ):
                    continue
                append = (
                    f"\n# Added by FreeAI after master-key confirmation (do not commit real keys).\n"
                    f"{line_key}{master_key}\n"
                )
                with env_path.open("a", encoding="utf-8") as f:
                    f.write(append)
                log.info("master_key_appended_to_dotenv", path=str(env_path))
            elif compose.is_file():
                body = (
                    "# Created by FreeAI after master-key confirmation.\n"
                    "# Add POSTGRES_PASSWORD and other variables (see backend/.env.example).\n"
                    f"{line_key}{master_key}\n"
                )
                env_path.write_text(body, encoding="utf-8")
                _chmod600(env_path)
                log.info("master_key_wrote_new_dotenv", path=str(env_path))
        except OSError as e:
            log.debug("dotenv_write_skipped", path=str(env_path), error=str(e))


def encrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return value
    if value.startswith(ENC_PREFIX):
        return value
    token = get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return ENC_PREFIX + token


def decrypt(value: Optional[str]) -> Optional[str]:
    if value is None or value == "":
        return value
    if not value.startswith(ENC_PREFIX):
        return value
    token = value[len(ENC_PREFIX):]
    try:
        return get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except MasterKeyNotReadyError:
        raise
    except InvalidToken:
        log.error(
            "decrypt_failed",
            reason="invalid_token — likely a rotated or mismatched master key",
            key_source="env" if os.environ.get("FREEAI_MASTER_KEY") else "file",
            key_path=str(MASTER_KEY_PATH),
        )
        return None


def hash_admin_token(plain: str) -> str:
    """bcrypt(SHA256(utf8(plain))) so tokens of any length work."""
    import bcrypt

    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode("ascii")


def mask_key(key: Optional[str]) -> Optional[str]:
    """Render an API key safely for the UI — only the last 4 chars are shown.

    Showing the provider prefix (sk-, gsk_, AIza…) would let an attacker
    who glimpses the UI confirm the key format during a bruteforce. Only
    the tail is exposed so an operator can still distinguish rotations.
    """
    if not key:
        return None
    if len(key) <= 4:
        return "•" * len(key)
    return f"{'•' * 8}{key[-4:]}"


def verify_admin_token_hash(plain: str, stored_hash: str) -> bool:
    import bcrypt

    if not stored_hash:
        return False
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    try:
        return bcrypt.checkpw(digest, stored_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False

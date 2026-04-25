"""Crypto — encryption round-trip and idempotence."""
from __future__ import annotations


def test_encrypt_decrypt_roundtrip():
    from app.crypto import decrypt, encrypt
    plain = "sk-secret-12345"
    encoded = encrypt(plain)
    assert encoded != plain
    assert encoded.startswith("enc::")
    assert decrypt(encoded) == plain


def test_encrypt_idempotent():
    from app.crypto import encrypt
    once = encrypt("hello")
    twice = encrypt(once)
    assert once == twice


def test_decrypt_legacy_plaintext_passes_through():
    from app.crypto import decrypt
    assert decrypt("plain-key") == "plain-key"


def test_confirm_pending_master_key_flow(tmp_path, monkeypatch):
    monkeypatch.delenv("FREEAI_MASTER_KEY", raising=False)
    from app import crypto as cr

    monkeypatch.setattr(cr, "MASTER_KEY_PATH", tmp_path / "m.key")
    monkeypatch.setattr(cr, "MASTER_KEY_PENDING_PATH", tmp_path / "m.pend")
    cr.clear_fernet_cache()
    from cryptography.fernet import Fernet

    raw = Fernet.generate_key().decode("ascii")
    cr.MASTER_KEY_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    cr.MASTER_KEY_PENDING_PATH.write_text(raw, encoding="utf-8")
    assert cr.master_key_confirmation_required() is True
    assert cr.confirm_pending_master_key("nope") is False
    assert cr.confirm_pending_master_key(raw) is True
    assert cr.is_master_key_ready()
    assert cr.decrypt(cr.encrypt("secret-value")) == "secret-value"


def test_mask_key():
    from app.crypto import mask_key
    assert mask_key(None) is None
    assert mask_key("") is None
    assert mask_key("abcd") == "••••"
    masked = mask_key("sk-supersecret-1234567890ab")
    assert "secret" not in masked
    # Prefix must NOT be exposed — only the tail is visible to the UI.
    assert not masked.startswith("sk-")
    assert masked.endswith("90ab")

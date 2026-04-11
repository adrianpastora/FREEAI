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


def test_mask_key():
    from app.crypto import mask_key
    assert mask_key(None) is None
    assert mask_key("") is None
    assert mask_key("short") == "•••••"
    masked = mask_key("sk-supersecret-1234567890ab")
    assert "secret" not in masked
    assert masked.startswith("sk-s")

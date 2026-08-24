"""Token envelope crypto tests (WP6) — pure, no Mongo."""

from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidTag

from pinner.crypto.tokens import (
    TokenDecryptionError,
    decrypt_token,
    encrypt_token,
    load_master_key,
    rotate_token,
)

KEY = bytes(range(32))  # deterministic test key
OTHER_KEY = bytes(range(32, 64))
SECRET = "pinterest-refresh-token-abc123"


def test_master_key_validation():
    assert load_master_key(KEY.hex()) == KEY
    with pytest.raises(ValueError):
        load_master_key("not-hex-zz")
    with pytest.raises(ValueError):
        load_master_key("abcd")  # too short
    with pytest.raises(ValueError):
        load_master_key(KEY.hex() + "ff")  # too long


def test_roundtrip():
    blob = encrypt_token(KEY, SECRET)
    assert blob["algo"] == "AES-256-GCM" and blob["key_version"] == 1
    assert decrypt_token(KEY, blob) == SECRET


def test_fresh_iv_per_encryption():
    a, b = encrypt_token(KEY, SECRET), encrypt_token(KEY, SECRET)
    assert a["iv"] != b["iv"] and a["ciphertext"] != b["ciphertext"]
    assert decrypt_token(KEY, a) == decrypt_token(KEY, b) == SECRET


def test_wrong_key_raises():
    blob = encrypt_token(KEY, SECRET)
    with pytest.raises(TokenDecryptionError):
        decrypt_token(OTHER_KEY, blob)


def test_tampered_ciphertext_raises():
    blob = encrypt_token(KEY, SECRET)
    raw = bytearray(base64.b64decode(blob["ciphertext"]))
    raw[0] ^= 0xFF
    blob["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(TokenDecryptionError):
        decrypt_token(KEY, blob)


def test_tampered_tag_and_iv_raise():
    blob = encrypt_token(KEY, SECRET)
    tampered_tag = dict(blob, auth_tag=base64.b64encode(b"\x00" * 16).decode())
    with pytest.raises(TokenDecryptionError):
        decrypt_token(KEY, tampered_tag)
    tampered_iv = dict(blob, iv=base64.b64encode(b"\x00" * 12).decode())
    with pytest.raises(TokenDecryptionError):
        decrypt_token(KEY, tampered_iv)


def test_malformed_envelopes_raise():
    blob = encrypt_token(KEY, SECRET)
    for broken in (
        {},
        {"ciphertext": blob["ciphertext"]},  # missing iv/tag
        dict(blob, ciphertext="!!!not-base64!!!"),
        dict(blob, iv=""),
        dict(blob, auth_tag=""),
        None,
    ):
        with pytest.raises(TokenDecryptionError):
            decrypt_token(KEY, broken)


def test_wrong_length_key_rejected_by_encrypt():
    with pytest.raises(ValueError):
        encrypt_token(b"short", SECRET)


def test_rotation_roundtrip():
    blob = encrypt_token(KEY, SECRET, key_version=1)
    rotated = rotate_token(KEY, OTHER_KEY, blob, new_version=2)
    assert rotated["key_version"] == 2
    assert decrypt_token(OTHER_KEY, rotated) == SECRET
    with pytest.raises(TokenDecryptionError):  # old key no longer opens it
        decrypt_token(KEY, rotated)


def test_underlying_primitive_is_gcm_authenticated():
    """Sanity: the envelope genuinely relies on GCM auth (not just b64)."""
    blob = encrypt_token(KEY, SECRET)
    with pytest.raises(InvalidTag):
        import base64 as b64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        AESGCM(KEY).decrypt(
            b64.b64decode(blob["iv"]),
            b64.b64decode(blob["ciphertext"]) + b"\x00" * 16,  # bogus tag
            None,
        )

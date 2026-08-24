"""AES-256-GCM envelope encryption for OAuth tokens at rest (WP6).

Mongo stores ONLY the envelope: {ciphertext, iv, auth_tag, key_version, algo}.
The master key lives exclusively in the environment (TOKEN_MASTER_KEY, 32-byte
hex / 64 hex chars) — never in Mongo, never in code, never in logs.

GCM gives authenticated encryption: any tampering (ciphertext, IV, or tag) and
any wrong key raise TokenDecryptionError — the system never silently returns
garbage credentials. Key rotation = bump key_version + re-encrypt sweep via
``rotate_token``.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALGO = "AES-256-GCM"
_TAG_BYTES = 16
_IV_BYTES = 12
_KEY_BYTES = 32


class TokenDecryptionError(Exception):
    """Raised on wrong key, tampering, or malformed envelope — never return
    garbage credentials."""


def load_master_key(hex_key: str) -> bytes:
    """Validate and decode the TOKEN_MASTER_KEY (64 hex chars -> 32 bytes)."""
    try:
        raw = bytes.fromhex(hex_key.strip())
    except ValueError as exc:
        raise ValueError("TOKEN_MASTER_KEY must be valid hex") from exc
    if len(raw) != _KEY_BYTES:
        raise ValueError(
            f"TOKEN_MASTER_KEY must be {_KEY_BYTES * 2} hex chars ({_KEY_BYTES} bytes)"
        )
    return raw


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)


def encrypt_token(master_key: bytes, plaintext: str, *, key_version: int = 1) -> dict:
    """Encrypt a secret into a storable envelope. Fresh random IV per call —
    encrypting the same token twice yields different envelopes (test-asserted)."""
    if len(master_key) != _KEY_BYTES:
        raise ValueError(f"master key must be {_KEY_BYTES} bytes")
    iv = os.urandom(_IV_BYTES)
    sealed = AESGCM(master_key).encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext, tag = sealed[:-_TAG_BYTES], sealed[-_TAG_BYTES:]
    return {
        "ciphertext": _b64e(ciphertext),
        "iv": _b64e(iv),
        "auth_tag": _b64e(tag),
        "key_version": key_version,
        "algo": ALGO,
    }


def decrypt_token(master_key: bytes, blob: dict) -> str:
    """Decrypt an envelope. Raises TokenDecryptionError on ANY failure mode."""
    try:
        ciphertext = _b64d(blob["ciphertext"])
        iv = _b64d(blob["iv"])
        tag = _b64d(blob["auth_tag"])
        if not iv or not tag:
            raise ValueError("empty iv or tag")
        plaintext = AESGCM(master_key).decrypt(iv, ciphertext + tag, None)
    except (KeyError, TypeError, ValueError, InvalidTag) as exc:
        raise TokenDecryptionError(
            f"token envelope invalid or wrong key ({type(exc).__name__})"
        ) from exc
    return plaintext.decode("utf-8")


def rotate_token(old_key: bytes, new_key: bytes, blob: dict, *, new_version: int) -> dict:
    """Re-encrypt an envelope under a new key/version (rotation sweep helper)."""
    return encrypt_token(new_key, decrypt_token(old_key, blob), key_version=new_version)

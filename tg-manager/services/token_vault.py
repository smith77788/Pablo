"""AES-256-GCM encryption for bot tokens and sensitive credentials at rest.

Usage:
    from services.token_vault import encrypt_token, decrypt_token

    enc = encrypt_token("1234567890:AAHxxxxxx")   # store in DB
    raw = decrypt_token(enc)                       # use for API calls
"""
from __future__ import annotations

import base64
import hashlib
import os

_MARKER = "ENC:"


def _key() -> bytes:
    """Derive 32-byte AES key from env var (or BOT_TOKEN as fallback)."""
    raw = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    if not raw:
        raw = os.environ.get("MANAGER_BOT_TOKEN", "changeme-set-TOKEN_ENCRYPTION_KEY")
    return hashlib.sha256(raw.encode()).digest()


def encrypt_token(token: str) -> str:
    """Encrypt *token*; returns 'ENC:<base64>' string safe for text column."""
    if not token or token.startswith(_MARKER):
        return token  # empty or already encrypted — pass through
    from Crypto.Cipher import AES as _AES

    nonce = os.urandom(12)
    cipher = _AES.new(_key(), _AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(token.encode())
    return _MARKER + base64.b64encode(nonce + tag + ct).decode()


def decrypt_token(enc: str) -> str:
    """Decrypt token. Returns plaintext. Falls back to input for backward compat."""
    if not enc:
        return enc
    if not enc.startswith(_MARKER):
        return enc  # plaintext (legacy row) — return as-is
    try:
        from Crypto.Cipher import AES as _AES

        raw = base64.b64decode(enc[len(_MARKER):])
        nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
        cipher = _AES.new(_key(), _AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag).decode()
    except Exception:
        return enc  # decryption failed — return raw value to avoid silent data loss

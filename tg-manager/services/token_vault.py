"""AES-256-GCM encryption for bot tokens and sensitive credentials at rest.

Usage:
    from services.token_vault import encrypt_token, decrypt_token

    enc = encrypt_token("1234567890:AAHxxxxxx")   # store in DB
    raw = decrypt_token(enc)                       # use for API calls
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import time

log = logging.getLogger(__name__)

_MARKER = "ENC:"

# Троттлинг предупреждения о сбое расшифровки. Неверный/ротированный
# TOKEN_ENCRYPTION_KEY роняет расшифровку КАЖДОЙ строки (сессии, токены, прокси)
# — без троттла один misconfig зальёт логи тысячами строк в секунду. Печатаем не
# чаще раза в минуту, но обязательно печатаем: тихий сбой ключа = весь флот молча
# «мёртв» без причины в логах.
_DECRYPT_WARN_INTERVAL = 60.0
_last_decrypt_warn = 0.0


def _warn_decrypt_failure() -> None:
    """Сообщить о провале расшифровки ENC:-строки (throttled). Секрет НЕ логируем."""
    global _last_decrypt_warn
    now = time.monotonic()
    if now - _last_decrypt_warn >= _DECRYPT_WARN_INTERVAL:
        _last_decrypt_warn = now
        log.warning(
            "token_vault: расшифровка ENC:-значения провалилась — возвращаю "
            "шифротекст как есть. Вероятно неверный/ротированный "
            "TOKEN_ENCRYPTION_KEY или повреждённая строка. Downstream сочтёт "
            "значение невалидным (сессия/токен/прокси не сработают). "
            "Сообщение троттлится до 1/мин.",
            exc_info=True,
        )


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
        # Значение ЯВНО помечено ENC:, но расшифровать не удалось — это не legacy
        # plaintext, а реальная ошибка (неверный ключ / порча). Сигналим (throttled)
        # и возвращаем исходное, чтобы не терять данные молча.
        _warn_decrypt_failure()
        return enc


def encrypt_bytes(data: bytes) -> bytes:
    """AES-256-GCM шифрование произвольных БАЙТ (для кусков файлов облака).
    Возвращает nonce(12) + tag(16) + ciphertext. Ключ — тот же _key()."""
    from Crypto.Cipher import AES as _AES
    nonce = os.urandom(12)
    cipher = _AES.new(_key(), _AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(data or b"")
    return nonce + tag + ct


def decrypt_bytes(blob: bytes) -> bytes:
    """Обратное к encrypt_bytes. Бросает при неверном ключе/порче (целостность
    важнее «тихого» возврата — для файлов молчаливая порча недопустима)."""
    from Crypto.Cipher import AES as _AES
    if not blob or len(blob) < 28:
        return b""
    nonce, tag, ct = blob[:12], blob[12:28], blob[28:]
    cipher = _AES.new(_key(), _AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ct, tag)


def session_fingerprint(session_str: str) -> str:
    """Детерминированный fingerprint сессии для ДЕДУПА (не для безопасности).

    encrypt_token недетерминирован (случайный nonce) → одинаковая сессия каждый
    раз шифруется по-разному, поэтому дедуп по равенству шифротекста невозможен.
    Здесь считаем стабильный sha256 от PLAINTEXT-сессии: если на вход пришла уже
    зашифрованная строка — сначала снимаем шифр, чтобы fp совпадал с plaintext.
    """
    if not session_str:
        return ""
    plain = decrypt_token(session_str) if session_str.startswith(_MARKER) else session_str
    return hashlib.sha256(plain.encode()).hexdigest()


def proxy_fingerprint(proxy_url: str) -> str:
    """Детерминированный fingerprint proxy_url для дедупа/ключей (тот же приём,
    что session_fingerprint): sha256 от PLAINTEXT. proxy_url — общий межтабличный
    ключ (UNIQUE user_proxies, PK infra_memory_proxies), а шифр недетерминирован,
    поэтому дедуп/ON CONFLICT переводятся на этот fp."""
    return session_fingerprint(proxy_url)

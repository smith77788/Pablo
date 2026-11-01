"""AES-256-GCM encryption for bot tokens and sensitive credentials at rest.

Usage:
    from services.token_vault import encrypt_token, decrypt_token

    enc = encrypt_token("1234567890:AAHxxxxxx")   # store in DB
    raw = decrypt_token(enc)                       # use for API calls
"""
from __future__ import annotations

import base64
import hashlib
import hmac
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


_DEFAULT_KEY = "changeme-set-TOKEN_ENCRYPTION_KEY"
_key_warned = False


def _key() -> bytes:
    """32-байтный ключ AES из окружения (запасной вариант — токен бота).

    Оба запасных варианта опасны молча, поэтому о них предупреждаем один раз
    за процесс:

    * без TOKEN_ENCRYPTION_KEY ключ выводится из токена бота — смена токена
      превращает все зашифрованные сессии, токены и прокси в мусор, причём не
      сразу, а при первом же обращении к ним;
    * без обеих переменных ключом становится строка из исходников, то есть
      шифрование «на диске» перестаёт быть шифрованием вовсе.
    """
    global _key_warned
    raw = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    if not raw:
        raw = os.environ.get("MANAGER_BOT_TOKEN", "")
        if raw:
            if not _key_warned:
                _key_warned = True
                log.warning(
                    "token_vault: TOKEN_ENCRYPTION_KEY не задан — ключ выводится "
                    "из токена бота. Смена токена бота сделает НЕЧИТАЕМЫМИ все "
                    "сохранённые сессии, токены и прокси. Задайте "
                    "TOKEN_ENCRYPTION_KEY отдельной переменной.")
        else:
            raw = _DEFAULT_KEY
            if not _key_warned:
                _key_warned = True
                log.error(
                    "token_vault: ни TOKEN_ENCRYPTION_KEY, ни MANAGER_BOT_TOKEN не "
                    "заданы — используется ключ по умолчанию из исходников. "
                    "Сессии и токены в базе фактически НЕ ЗАШИФРОВАНЫ. Задайте "
                    "TOKEN_ENCRYPTION_KEY немедленно.")
    return hashlib.sha256(raw.encode()).digest()


def _decrypt_keys() -> list[bytes]:
    """Ключи, которыми пробуем РАСШИФРОВАТЬ: текущий первым, затем прежние.

    Зачем связка. Ключ один на все секреты (сессии, токены ботов, прокси,
    файлы облака), и номера ключа в шифротексте нет. Пока расшифровка знала
    только текущий ключ, сменить его было нельзя вообще: новое значение
    TOKEN_ENCRYPTION_KEY превращало всё хранилище в мусор разом. Теперь
    смена ключа — это три шага без потери данных:

      1. новый ключ в TOKEN_ENCRYPTION_KEY, старый — в
         TOKEN_ENCRYPTION_KEYS_PREVIOUS (через запятую, если их несколько);
      2. приложение читает старые строки прежним ключом, а пишет уже новым;
         deploy/scripts/rotate_vault_key.py --apply перешифровывает остаток
         базы одним проходом (reencrypt_token ниже);
      3. когда скрипт в dry-run показывает ноль строк, старый ключ убирается
         из TOKEN_ENCRYPTION_KEYS_PREVIOUS. Исключение — файлы облака
         (encrypt_bytes): они лежат вне базы, скрипт их не видит, и пока они
         нужны, прежний ключ должен оставаться в связке.

    Отдельно: если TOKEN_ENCRYPTION_KEY задан, а MANAGER_BOT_TOKEN тоже есть,
    ключ, выведенный из токена бота, остаётся в связке на чтение. Это путь
    ухода с «ключа из токена бота» (о нём предупреждает _key) на отдельную
    переменную: раньше её установка задним числом делала нечитаемым всё, что
    успели зашифровать до неё.

    Перебор ключей безопасен: GCM проверяет тег, и чужой ключ даёт отказ, а не
    правдоподобный мусор.
    """
    keys = [_key()]
    for raw in os.environ.get("TOKEN_ENCRYPTION_KEYS_PREVIOUS", "").split(","):
        raw = raw.strip()
        if raw:
            keys.append(hashlib.sha256(raw.encode()).digest())
    if os.environ.get("TOKEN_ENCRYPTION_KEY", ""):
        bot_raw = os.environ.get("MANAGER_BOT_TOKEN", "")
        if bot_raw:
            keys.append(hashlib.sha256(bot_raw.encode()).digest())
    seen: set[bytes] = set()
    out: list[bytes] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _open_gcm(raw: bytes) -> tuple[bytes, int]:
    """Расшифровать nonce|tag|ct первым подходящим ключом связки.

    Возвращает (plaintext, номер ключа в связке): 0 — текущий ключ, больше —
    прежний (строку пора перешифровать). Бросает, если не подошёл ни один.
    """
    from Crypto.Cipher import AES as _AES

    nonce, tag, ct = raw[:12], raw[12:28], raw[28:]
    last_exc: Exception | None = None
    for idx, key in enumerate(_decrypt_keys()):
        try:
            cipher = _AES.new(key, _AES.MODE_GCM, nonce=nonce)
            return cipher.decrypt_and_verify(ct, tag), idx
        except Exception as exc:  # неверный тег — пробуем следующий ключ
            last_exc = exc
    raise last_exc or ValueError("token_vault: нет ключей для расшифровки")


def _encrypt_plaintext(token: str) -> str:
    from Crypto.Cipher import AES as _AES

    nonce = os.urandom(12)
    cipher = _AES.new(_key(), _AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(token.encode())
    return _MARKER + base64.b64encode(nonce + tag + ct).decode()


def encrypt_token(token: str) -> str:
    """Encrypt *token*; returns 'ENC:<base64>' string safe for text column."""
    if not token or token.startswith(_MARKER):
        return token  # empty or already encrypted — pass through
    return _encrypt_plaintext(token)


def encrypt_plaintext_token(token: str) -> str:
    """Зашифровать заведомый plaintext, даже если он начинается с ``ENC:``."""
    return _encrypt_plaintext(token) if token else token


def decrypt_token(enc: str) -> str:
    """Decrypt token. Returns plaintext. Falls back to input for backward compat."""
    if not enc:
        return enc
    if not enc.startswith(_MARKER):
        return enc  # plaintext (legacy row) — return as-is
    try:
        raw = base64.b64decode(enc[len(_MARKER):])
        plain, _idx = _open_gcm(raw)
        return plain.decode()
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
    if not blob or len(blob) < 28:
        return b""
    plain, _idx = _open_gcm(blob)
    return plain


def needs_reencrypt(enc: str) -> bool:
    """Зашифрована ли строка НЕ текущим ключом (то есть её пора перешифровать).

    False для пустых, plaintext-строк (их шифрует encrypt_legacy_secrets без
    --rotate) и строк, которые не открывает ни один ключ: перешифровать такую
    нечем, и выдавать её за «готовую к ротации» было бы враньём.
    """
    if not enc or not enc.startswith(_MARKER):
        return False
    try:
        _plain, idx = _open_gcm(base64.b64decode(enc[len(_MARKER):]))
    except Exception:
        return False
    return idx > 0


def reencrypt_token(enc: str) -> str:
    """Перешифровать строку текущим ключом, если она открыта прежним.

    Строка на текущем ключе, plaintext и нечитаемые возвращаются как есть —
    функция никогда не теряет данные и безопасна для повторного прогона.
    """
    if not needs_reencrypt(enc):
        return enc
    plain, _idx = _open_gcm(base64.b64decode(enc[len(_MARKER):]))
    return _encrypt_plaintext(plain.decode())


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


def keyed_fingerprint(value: str, *, namespace: str) -> str:
    """Стабильный HMAC для дедупа секретных данных без offline-перебора.

    Обычный sha256 подходит для высокоэнтропийных session/proxy значений, но
    пароль пользователя часто короткий. HMAC не позволяет проверить догадку о
    пароле без серверного ключа шифрования.
    """
    payload = f"{namespace}\0{value}".encode()
    return hmac.new(_key(), payload, hashlib.sha256).hexdigest()

"""Telegram Mini App — initData validation and session tokens."""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Optional

log = logging.getLogger(__name__)

# Ниже этой длины строка заведомо не токен бота (настоящий — около 46 символов:
# "1234567890:AA...").
_MIN_SIGNING_SECRET = 16


@functools.lru_cache(maxsize=1)
def _complain_no_secret() -> None:
    """Пожаловаться один раз за процесс — иначе зальём лог на каждом запросе."""
    log.error(
        "mini_app_auth: BOT_TOKEN не задан (или не похож на токен) — вход в "
        "мини-апп отключён. Подпись сессий выводится из токена бота; из пустой "
        "строки получается общеизвестный ключ, и тогда сессию подделывает кто "
        "угодно от имени любого владельца. Задайте BOT_TOKEN (или "
        "MANAGER_BOT_TOKEN) и перезапустите."
    )


def signing_secret_ok(secret: Optional[str]) -> bool:
    """Годится ли строка как ключ подписи входа.

    Ключ выводится из токена бота: sha256(token). Пустая строка — тоже «ключ»,
    только общеизвестный: sha256(b"") — константа, подпись с ней подделывается
    за секунду. Дальше это полный обход входа: злоумышленник подписывает токен
    с ЛЮБЫМ user_id и работает от имени любого владельца — его аккаунты, строки
    сессий, операции, платежи. Хуже всего, что снаружи всё выглядит рабочим:
    мини-апп открывается, вход «проходит».

    Пустой токен — не теория. Веб-роль (INFRAGRAM_ROLE=web) поднимается
    отдельным процессом, переменную легко не донести до неё при редеплое или
    переименовании, и os.getenv молча вернёт "".

    Поэтому пустой (или явно не похожий на токен) секрет — отказ, а не тихая
    работа с нулевым ключом.
    """
    if not secret or len(secret.strip()) < _MIN_SIGNING_SECRET:
        _complain_no_secret()
        return False
    return True


def validate_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Validate Telegram Mini App initData using HMAC-SHA256.
    Returns user dict on success, None on failure.
    """
    if not signing_secret_ok(bot_token):
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_value = parsed.pop("hash", None)
        if not hash_value:
            return None
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, hash_value):
            return None
        auth_date = int(parsed.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            return None
        user = json.loads(parsed.get("user", "{}"))
        return {
            "user_id": int(user.get("id", 0)),
            "username": user.get("username", ""),
            "first_name": user.get("first_name", ""),
        }
    except Exception:
        return None


def make_token(user_id: int, bot_token: str) -> str:
    """Generate a 2-hour session token."""
    if not signing_secret_ok(bot_token):
        # Выдать токен, подписанный общеизвестным ключом, хуже чем не выдать
        # ничего: он выглядит настоящим и открывает вход кому угодно.
        raise ValueError("нет ключа подписи: BOT_TOKEN не задан")
    ts = int(time.time())
    payload = f"{user_id}:{ts}"
    secret = hashlib.sha256(bot_token.encode()).digest()
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}:{sig}"


def parse_token(token: str, bot_token: str, max_age: int = 7200) -> Optional[int]:
    """Validate a session token, return user_id or None."""
    if not signing_secret_ok(bot_token):
        return None
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return None
        uid, ts_s, sig = parts
        if time.time() - int(ts_s) > max_age:
            return None
        payload = f"{uid}:{ts_s}"
        secret = hashlib.sha256(bot_token.encode()).digest()
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(expected, sig):
            return None
        return int(uid)
    except Exception:
        return None


# ── Пропуск для одной ссылки ────────────────────────────────────────────────
#
# Токен сессии раньше ездил в строке запроса (`?token=`): так открывались
# выгрузки во внешнем браузере (`tg.openLink`), нативное сохранение файла
# (`tg.downloadFile`) и живой поток (EventSource не умеет заголовки). Строка
# запроса оседает в журнале доступа aiohttp, в логах платформы и в истории
# внешнего браузера — то есть двухчасовой ключ ко ВСЕМ данным владельца
# лежал там, куда владелец не заглядывает. SECURITY.md это прямо запрещает.
#
# Пропуск отличается от токена тремя вещами: подписан с другой меткой (токеном
# сессии его не подменить и наоборот), привязан к ОДНОМУ пути и живёт столько,
# сколько нужно одной ссылке. Утёкший пропуск выгрузки через две минуты мёртв
# и ничего, кроме этой выгрузки, не открывал.

URL_TICKET_TTL = 120
# EventSource переподключается САМ, повторяя тот же адрес, поэтому пропуск
# потока живёт как сессия; сервер всё равно закрывает поток по его сроку.
EVENTS_PATH = "/api/miniapp/events"
EVENTS_TICKET_TTL = 7200


def url_ticket_ttl(path: str) -> int:
    return EVENTS_TICKET_TTL if path == EVENTS_PATH else URL_TICKET_TTL


def _ticket_sig(uid: int, ts: int, path: str, bot_token: str) -> str:
    secret = hashlib.sha256(bot_token.encode()).digest()
    msg = f"url-ticket|{uid}|{ts}|{path}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]


def make_url_ticket(user_id: int, bot_token: str, path: str) -> str:
    """Пропуск на GET одного пути. Формат `uid.ts.sig` — без двоеточий, чтобы
    parse_token не принял его за токен сессии."""
    if not signing_secret_ok(bot_token):
        raise ValueError("нет ключа подписи: BOT_TOKEN не задан")
    ts = int(time.time())
    return f"{int(user_id)}.{ts}.{_ticket_sig(int(user_id), ts, path, bot_token)}"


def parse_url_ticket(ticket: str, bot_token: str, path: str) -> Optional[int]:
    """uid, если пропуск подписан для ЭТОГО пути и не истёк; иначе None."""
    if not signing_secret_ok(bot_token) or not ticket:
        return None
    try:
        uid_s, ts_s, sig = ticket.split(".")
        uid, ts = int(uid_s), int(ts_s)
        if not 0 <= time.time() - ts <= url_ticket_ttl(path):
            return None
        if not hmac.compare_digest(_ticket_sig(uid, ts, path, bot_token), sig):
            return None
        return uid
    except Exception:
        return None

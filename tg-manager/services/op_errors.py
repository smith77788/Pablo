"""Классификация ошибок операций и нормализация их результата.

Выделено из op_worker.py (распил монолита): это чистая, не имеющая состояния
логика «что за ошибка и как на неё реагировать» (retry/flood/fatal/skip, сеть/
прокси, мёртвая сессия vs временный конфликт двух IP) плюс канонизация результата
exec-функции. Держать её отдельно от 12k-строчного исполнителя проще для чтения и
тестов, а op_worker импортирует эти имена обратно — публичный/приватный контракт
(op_worker._classify_op_error и т.п.) не меняется.

Никакого состояния уровня модуля здесь нет — только константы-паттерны и функции.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


# ── Retry Intelligence: паттерны и классы ошибок ───────────────────────────────
_RETRYABLE_ERRORS = {
    "TimeoutError",
    "ConnectionError",
    "NetworkError",
    "ConnectionResetError",
    "ServerError",
    "OSError",
    "asyncio.TimeoutError",
    "TelegramNetworkError",
}
_FATAL_ERRORS = {
    "AuthKeyUnregisteredError",
    # Истёкшая и невалидная авторизация не оживают от повтора: без них операция
    # уходила в 'retry' и била в мёртвую сессию до конца своих попыток.
    "SessionExpiredError",
    "AuthKeyInvalidError",
    "SessionRevokedError",
    "UserDeactivatedBan",
    "UserDeactivatedError",
    "BotKicked",
    "PhoneNumberBanned",
    "UserBannedInChannel",
    "ChannelBannedError",
    "ChatWriteForbiddenError",
}
# Fatal message fragments — if present in exception message, never retry
_FATAL_MSG_PATTERNS = re.compile(
    # NB: AUTH_KEY_DUPLICATED сюда НЕ входит — это временный конфликт двух IP, а не
    # фатал (см. _is_session_conflict_error): деактивировать аккаунт из-за него нельзя.
    r"USER_DEACTIVATED|ACCOUNT_BANNED|USER_BANNED|SESSION_PASSWORD_NEEDED|"
    r"PHONE_NUMBER_BANNED|BOT_KICKED|CHANNEL_BANNED|"
    r"permanently banned|account is banned",
    re.IGNORECASE,
)
_FLOOD_PATTERNS = re.compile(r"flood.wait|FLOOD_WAIT|FloodWait", re.IGNORECASE)
_PEER_FLOOD_PATTERNS = re.compile(r"peer.flood|PEER_FLOOD|PeerFlood", re.IGNORECASE)
_NETWORK_PATTERNS = re.compile(
    r"connection to telegram failed|general socks server failure|proxy недоступен|"
    r"timeout при подключении|ошибка сети|"
    r"connection reset|connection refused|connection timed out|"
    r"network is unreachable|broken pipe|eof occurred|"
    r"socks5|socks4|proxy error|proxy connect|"
    r"OSError|TimeoutError|ConnectionReset|ConnectionRefused|"
    r"timed out|could not connect|failed to connect|"
    r"connection aborted|no route to host|transport closed",
    re.IGNORECASE,
)

# AUTH_KEY_DUPLICATED / «used under two different IP addresses simultaneously» — это
# НЕ смерть сессии, а ВРЕМЕННЫЙ конфликт: одна и та же сессия секунду коннектилась с
# двух IP (аккаунт кратко был онлайн на телефоне/другом устройстве, либо дёрнулся
# прокси и IP на миг разъехался). Telegram гасит одно из соединений, но сессия обычно
# оживает, когда конфликт уходит. Такой аккаунт НЕЛЬЗЯ деактивировать — только
# остудить и повторить. Многосессионность у Telegram штатная: у каждого устройства
# СВОЙ auth_key, и наличие сессии на телефоне само по себе конфликта не вызывает —
# его вызывает лишь ОБЩАЯ (импортированная) сессия, используемая с двух IP разом.
_SESSION_CONFLICT_PATTERNS = re.compile(
    r"AUTH_KEY_DUPLICATED|AuthKeyDuplicated|two different IP",
    re.IGNORECASE,
)
# Мёртвая сессия: ключ отозван, аккаунт удалён или номер забанен. Такой аккаунт
# деактивируется (is_active=FALSE) и требует переимпорта — в отличие от конфликта
# двух IP выше, который лечится кулдауном.
#
# Паттерны держим в ДВУХ формах, потому что сюда приходят обе. Код ошибки
# (AUTH_KEY_UNREGISTERED, SESSION_REVOKED) виден там, где текст собирали мы сами
# или где передали имя класса. Но на обычном пути исполнители передают
# `str(exc)`, а это ЧЕЛОВЕЧЕСКОЕ сообщение Telethon, в котором кода нет вообще:
# проверено на telethon 1.45.0, SESSION_REVOKED выглядит как «The authorization
# has been invalidated, because of the user terminating all sessions», а
# USER_DEACTIVATED — как «The user has been deleted/deactivated». Пока здесь были
# только коды, восемь из девяти смертей сессии детектор не видел: аккаунт с
# отозванным ключом оставался активным, каждая следующая операция брала его,
# коннектилась и падала, а владелец видел «ошибка» вместо «переимпортируйте
# аккаунт».
#
# Фразы выбраны узкие намеренно. Раньше здесь стояло «authorization key», и это
# ловило AuthKeyPermEmptyError («The method is unavailable for temporary
# authorization key») — сессия там ЖИВА, метод просто недоступен временному
# ключу, а аккаунт за это деактивировался. Тот же класс вреда, что у конфликта
# двух IP, который уже чинили.
_DEAD_SESSION_PATTERNS = re.compile(
    # Коды и имена классов.
    r"AUTH_KEY_UNREGISTERED|AUTH_KEY_INVALID|SESSION_REVOKED|SESSION_EXPIRED|"
    r"USER_DEACTIVATED|PHONE_NUMBER_BANNED|different data center|"
    r"AuthKeyUnregistered|AuthKeyInvalid|SessionRevoked|SessionExpired|"
    r"UserDeactivated|PhoneNumberBanned|"
    # Человеческие сообщения Telethon (1.45.0).
    r"key is not registered|registered in the system|the key is invalid|"
    r"deleted/deactivated|authorization has been invalidated|"
    r"authorization has expired|banned from telegram",
    re.IGNORECASE,
)


def is_dead_session_text(error_text: str | None) -> bool:
    """Мёртвая ли сессия по тексту ошибки. ЕДИНАЯ ДВЕРЬ этого решения.

    Конфликт двух IP (AUTH_KEY_DUPLICATED) мёртвой сессией НЕ считается: см.
    _SESSION_CONFLICT_PATTERNS выше и tests/test_auth_key_duplicated_not_dead.py.
    Без метрики и без побочных эффектов — её считает `_is_dead_session_error`.
    """
    if not error_text:
        return False
    if _is_session_conflict_error(error_text):
        return False
    return bool(_DEAD_SESSION_PATTERNS.search(error_text))


def _is_session_conflict_error(error_text: str) -> bool:
    return bool(_SESSION_CONFLICT_PATTERNS.search(error_text or ""))


def _normalize_result(result: dict, op_type: str, duration_s: float) -> dict:
    """Обеспечить единый формат результата операции для хранения и отчётов.

    Канонические поля: status, ok, failed, total, summary, duration_s.
    Существующие алиасы (sent, created) нормализуются в ok.
    """
    if not isinstance(result, dict):
        result = {"status": "done", "summary": str(result)}

    # Нормализация ok: разные exec-функции используют sent/ok/created
    if "ok" not in result:
        for alias in ("sent", "created", "waves_completed", "left", "deleted", "joined", "invited", "published"):
            if alias in result:
                result["ok"] = result[alias]
                break
        else:
            result["ok"] = 0

    if "failed" not in result:
        # Историч. разнобой: ~25 exec-функций отдают счётчик провалов под ключом
        # "fail", а не каноническим "failed". Нормализуем алиас (как для ok выше).
        # Без этого полностью провальная операция (ok=0, ВСЕ цели упали) приходила
        # в нормализатор статуса как ok=0/failed=0 → помечалась "done" (успех):
        # пользователь видел успех у пустой рассылки, а circuit breaker и pacing
        # получали ложный сигнал успеха (подрыв анти-детекта).
        result["failed"] = int(result.get("fail", 0) or 0)

    if "total" not in result:
        result["total"] = result.get("ok", 0) + result.get("failed", 0)

    if "summary" not in result or not result["summary"]:
        ok = result.get("ok", 0)
        failed = result.get("failed", 0)
        result["summary"] = f"✅ {ok} успешно, ❌ {failed} ошибок"

    result["duration_s"] = round(duration_s, 1)
    result["op_type"] = op_type
    return result


def _classify_op_error(exc: Exception) -> str:
    """Классифицирует ошибку операции: 'retry' | 'flood' | 'fatal' | 'skip'."""
    name = type(exc).__name__
    msg = str(exc)
    # AUTH_KEY_DUPLICATED (конфликт двух IP) — НЕ фатал: сессия временно
    # конфликтует, а не мертва. Повторяем (после кулдауна аккаунта), не деактивируем.
    # Проверяем ПЕРЕД fatal, иначе бы поймалось на "AUTH_KEY" in msg ниже.
    if _is_session_conflict_error(msg):
        return "retry"
    # Fatal: known class names OR fatal message patterns — do NOT retry these
    if (
        name in _FATAL_ERRORS
        or "SESSION_REVOKED" in msg
        or "AUTH_KEY" in msg
        or _FATAL_MSG_PATTERNS.search(msg)
    ):
        return "fatal"
    if _PEER_FLOOD_PATTERNS.search(msg) or _PEER_FLOOD_PATTERNS.search(name):
        return "peer_flood"
    if _FLOOD_PATTERNS.search(msg) or _FLOOD_PATTERNS.search(name):
        return "flood"
    if (
        name in _RETRYABLE_ERRORS
        or "timeout" in msg.lower()
        or "connection" in msg.lower()
    ):
        return "retry"
    if (
        "CHANNEL_PRIVATE" in msg
        or "CHAT_ADMIN_REQUIRED" in msg
        or "ChatAdminRequired" in name
    ):
        return "skip"
    return "retry"


def _is_network_or_proxy_error(error_text: str) -> bool:
    # Конфликт двух IP лечится как транспортная проблема: остудить аккаунт и
    # повторить (а НЕ деактивировать) — поэтому включаем его сюда.
    return bool(_NETWORK_PATTERNS.search(error_text)) or _is_session_conflict_error(error_text)


def _is_dead_session_error(error_text: str) -> bool:
    # Решение о смерти сессии принимает is_dead_session_text (единая дверь);
    # здесь к нему добавлены только метрики.
    #
    # AUTH_KEY_DUPLICATED матчится общим паттерном (AUTH_KEY/AuthKeyDuplicated), но это
    # ВРЕМЕННЫЙ конфликт, а не мёртвая сессия — исключаем, чтобы аккаунт не
    # деактивировался (is_active=FALSE) из-за кратковременного пересечения IP.
    if _is_session_conflict_error(error_text):
        # Конфликт двух IP — считаем ОТДЕЛЬНО: это ранний сигнал, что где-то
        # нарушен захват аккаунта (находка №1), а не гибель сессии.
        try:
            from services import metrics as _m
            _m.inc("infragram_session_deaths_total", {"kind": "auth_key_conflict"})
        except Exception:
            pass
        return False
    dead = is_dead_session_text(error_text)
    if dead:
        # Метрика (аудит №6): смерть сессии — самая дорогая потеря продукта,
        # и до сих пор она нигде не считалась.
        try:
            from services import metrics as _m
            _m.inc("infragram_session_deaths_total", {"kind": "dead_session"})
        except Exception:
            pass
    return dead

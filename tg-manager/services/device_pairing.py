"""Связывание устройства — автономный вход вне Telegram (PWA, потом Android).

Мини-апп входит только через Telegram initData. Отдельному приложению его взять
неоткуда, но и заводить второй логин с паролем незачем: точка доверия остаётся
одна — Telegram. Поток:

1. в боте владелец берёт ОДНОРАЗОВЫЙ КОД связывания (живёт минуты);
2. приложение меняет код на долгоживущий ТОКЕН УСТРОЙСТВА (30 дней);
3. приложение меняет токен устройства на обычный короткий сессионный токен —
   тот же, что выдаёт Telegram-вход, поэтому остальные ~500 маршрутов API не
   меняются вовсе.

Здесь только чистое ядро (код, подпись/проверка токена — его и проверяют тесты)
и тонкие обёртки над БД. Токен устройства самоподписан HMAC на серверном
секрете (ключ бота, как у сессионного токена) — проверка не ходит в БД; в БД
хранится лишь ОТПЕЧАТОК токена (sha256), чтобы устройство можно было отозвать,
а утечка таблицы не отдавала действующий доступ.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

log = logging.getLogger(__name__)

# Код связывания: без похожих символов (нет 0/O, 1/I/L) — вводится с телефона.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LEN = 8
CODE_TTL_SEC = 300                     # код живёт 5 минут
DEVICE_TTL_DAYS = 30                   # токен устройства живёт 30 дней
_TOKEN_PREFIX = "dev"


def new_code() -> str:
    """Одноразовый код связывания, сгруппированный для читаемости: XXXX-XXXX."""
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str | None) -> str:
    """Привести введённый код к каноническому виду: верхний регистр, только
    буквы алфавита, с дефисом. Пользователь мог ввести без дефиса/в нижнем
    регистре/с пробелами — принимаем."""
    if not code:
        return ""
    up = "".join(ch for ch in str(code).upper() if ch in _ALPHABET)
    if len(up) != _CODE_LEN:
        return ""
    return f"{up[:4]}-{up[4:]}"


def make_device_token(owner_id: int, secret: str, *,
                      nonce: str | None = None, ts: int | None = None) -> str:
    """Долгоживущий самоподписанный токен устройства.

    Формат: dev:{owner_id}:{ts}:{nonce}:{sig}. nonce делает каждый токен
    уникальным (разные устройства → разные отпечатки, независимый отзыв).
    """
    ts = int(time.time()) if ts is None else int(ts)
    nonce = nonce or secrets.token_hex(8)
    payload = f"{owner_id}:{ts}:{nonce}"
    sig = _sign(payload, secret)
    return f"{_TOKEN_PREFIX}:{payload}:{sig}"


def parse_device_token(token: str, secret: str, *,
                       max_age_days: int = DEVICE_TTL_DAYS,
                       now: float | None = None) -> int | None:
    """Проверить токен устройства → owner_id или None (подпись/срок/формат)."""
    try:
        parts = (token or "").split(":")
        if len(parts) != 5 or parts[0] != _TOKEN_PREFIX:
            return None
        _p, uid, ts_s, nonce, sig = parts
        payload = f"{uid}:{ts_s}:{nonce}"
        expected = _sign(payload, secret)
        if not hmac.compare_digest(expected, sig):
            return None
        age = (time.time() if now is None else now) - int(ts_s)
        if age > max_age_days * 86400 or age < -300:   # -300: терпим рассинхрон часов
            return None
        return int(uid)
    except (ValueError, TypeError):
        return None


def token_fingerprint(token: str) -> str:
    """Отпечаток токена для хранения/отзыва (сам токен в БД не кладём)."""
    return hashlib.sha256((token or "").encode()).hexdigest()


def _sign(payload: str, secret: str) -> str:
    key = hashlib.sha256((secret or "").encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]


# ── Обёртки над БД (тонкие; логика — выше) ─────────────────────────────────

async def create_pairing_code(pool, owner_id: int, *, ttl_sec: int = CODE_TTL_SEC) -> str:
    """Завести одноразовый код связывания для владельца. Возвращает код.

    Старые непогашенные коды владельца гасим — активным остаётся один (последний),
    чтобы «покажи код ещё раз» не плодило действующие коды.
    """
    code = new_code()
    await pool.execute(
        "UPDATE device_pairings SET code=NULL "
        "WHERE owner_id=$1 AND code IS NOT NULL AND paired_at IS NULL", owner_id)
    await pool.execute(
        "INSERT INTO device_pairings(owner_id, code, code_expires_at) "
        "VALUES($1,$2, now() + ($3 || ' seconds')::interval)",
        owner_id, code, str(int(ttl_sec)))
    return code


async def redeem_code(pool, code: str, *, label: str = "") -> dict | None:
    """Атомарно погасить код. Возвращает {"owner_id","id"} или None (код не
    найден/истёк/уже погашен). Гонку двух обменов закрывает сам UPDATE ...
    WHERE code=$1: второй уже не найдёт код. Отпечаток токена проставляется
    вторым шагом (attach_token_fp), т.к. токен зависит от owner_id, известного
    только после погашения."""
    norm = normalize_code(code)
    if not norm:
        return None
    row = await pool.fetchrow(
        "UPDATE device_pairings "
        "   SET code=NULL, device_label=$2, paired_at=now() "
        " WHERE code=$1 AND code_expires_at > now() AND paired_at IS NULL "
        " RETURNING owner_id, id", norm, (label or "")[:80])
    return {"owner_id": int(row["owner_id"]), "id": int(row["id"])} if row else None


async def attach_token_fp(pool, pairing_id: int, token_fp: str) -> None:
    """Привязать отпечаток выданного токена к погашенной записи связывания."""
    await pool.execute(
        "UPDATE device_pairings SET token_fp=$2 WHERE id=$1",
        int(pairing_id), token_fp)


async def device_active(pool, token_fp: str) -> bool:
    """Устройство с этим отпечатком связано и НЕ отозвано (fail-closed: при сбое
    считаем неактивным — токен без записи не должен работать)."""
    try:
        row = await pool.fetchrow(
            "SELECT 1 FROM device_pairings "
            "WHERE token_fp=$1 AND revoked_at IS NULL LIMIT 1", token_fp)
        return bool(row)
    except Exception:
        log.debug("device_pairing: device_active failed", exc_info=True)
        return False


async def touch_device(pool, token_fp: str) -> None:
    try:
        await pool.execute(
            "UPDATE device_pairings SET last_seen_at=now() WHERE token_fp=$1", token_fp)
    except Exception:
        pass


async def list_devices(pool, owner_id: int) -> list[dict]:
    try:
        rows = await pool.fetch(
            "SELECT id, device_label, paired_at, last_seen_at, revoked_at "
            "FROM device_pairings WHERE owner_id=$1 AND paired_at IS NOT NULL "
            "ORDER BY paired_at DESC LIMIT 50", owner_id)
        return [dict(r) for r in (rows or [])]
    except Exception:
        return []


async def revoke_device(pool, owner_id: int, pairing_id: int) -> bool:
    """Отозвать устройство владельца. Возвращает True, если что-то отозвали."""
    try:
        row = await pool.fetchrow(
            "UPDATE device_pairings SET revoked_at=now() "
            "WHERE id=$1 AND owner_id=$2 AND revoked_at IS NULL RETURNING id",
            int(pairing_id), owner_id)
        return bool(row)
    except Exception:
        return False

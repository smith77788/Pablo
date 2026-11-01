"""Личные API-ключи владельца — выдача, отзыв и проверка на входе в REST API.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. До него ключи выдавали ДВА места (бот, «API Hub», и
мини-апп, экран «API Ключи»), каждое по-своему: бот с приставкой `bm_`, мини-апп
без неё, с разным префиксом в базе. А REST API (`services/rest_api.py`) не
принимал ни тот, ни другой — пускал только общий ADMIN_SECRET. Владелец получал
ключ, видел «Сохраните ключ — он показывается только один раз», шёл с ним в n8n
и получал 401. Выдача, проверка и показ ключа теперь живут здесь, в одном месте.

Хранится только sha256 ключа: сам ключ виден ОДИН раз, при выдаче. Ключи,
выданные до этого модуля (с `bm_` и без), продолжают работать: хэш считается от
строки, которую владелец пришлёт, ровно как при выдаче.

ОБЛАСТИ. `read` — чтение (аккаунты, операции, переписка), `write` — действия от
имени аккаунтов (отправка сообщения, нажатие кнопки). Ключ «только чтение»
нужен для отчётов и таблиц: утечка такого ключа не даёт писать людям от имени
аккаунтов владельца.
"""
from __future__ import annotations

import hashlib
import logging
import secrets

log = logging.getLogger(__name__)

KEY_PREFIX = "bm_"
MAX_ACTIVE_KEYS = 5
SCOPES = ("read", "write")
SCOPE_LABELS = {"read": "чтение", "write": "действия от имени аккаунтов"}


def hash_key(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def display_prefix(raw: str) -> str:
    """То, что хранится и показывается вместо ключа: `bm_` и восемь знаков."""
    return raw[: len(KEY_PREFIX) + 8]


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def normalize_scopes(scopes) -> list[str]:
    """Области из ввода → упорядоченный список известных.

    Пустой/непонятный ввод даёт обе области — так ключ вёл себя всегда, и
    молча выдать ключ, который ничего не может, хуже. `write` без `read` не
    бывает: действие от имени аккаунта бессмысленно без права увидеть аккаунт.
    """
    if isinstance(scopes, str):
        scopes = [s.strip() for s in scopes.split(",")]
    got = {str(s).strip().lower() for s in (scopes or []) if s}
    got &= set(SCOPES)
    if not got:
        return list(SCOPES)
    if "write" in got:
        got.add("read")
    return [s for s in SCOPES if s in got]


async def count_active(pool, owner_id: int) -> int:
    return int(await pool.fetchval(
        "SELECT COUNT(*) FROM api_keys WHERE user_id=$1 AND is_active=TRUE",
        owner_id) or 0)


async def create_key(pool, owner_id: int, name: str, scopes=None) -> dict:
    """Выдать ключ. Возвращает {"id", "key", "prefix", "name", "scopes"}.

    Сырой ключ — только в ответе этой функции. ValueError с русским текстом —
    когда ключей уже предельное число.
    """
    name = (name or "").strip()[:64] or "Ключ API"
    scopes_l = normalize_scopes(scopes)
    if await count_active(pool, owner_id) >= MAX_ACTIVE_KEYS:
        raise ValueError(
            f"Активных ключей уже {MAX_ACTIVE_KEYS} — отзовите ненужный и повторите")
    raw = generate_key()
    prefix = display_prefix(raw)
    row = await pool.fetchrow(
        """INSERT INTO api_keys (user_id, key_hash, key_prefix, name, scopes)
           VALUES ($1, $2, $3, $4, $5::text[]) RETURNING id""",
        owner_id, hash_key(raw), prefix, name, scopes_l)
    return {"id": row["id"], "key": raw, "prefix": prefix, "name": name,
            "scopes": scopes_l}


async def list_keys(pool, owner_id: int, *, active_only: bool = False) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, name, key_prefix, is_active, created_at, last_used_at,
                  requests_total, COALESCE(scopes, ARRAY['read','write']) AS scopes
           FROM api_keys WHERE user_id=$1"""
        + (" AND is_active=TRUE" if active_only else "")
        + " ORDER BY is_active DESC, created_at DESC LIMIT 100",
        owner_id)
    return [dict(r) for r in rows]


async def revoke_key(pool, owner_id: int, key_id: int) -> bool:
    res = await pool.execute(
        """UPDATE api_keys SET is_active=FALSE, revoked_at=now()
           WHERE id=$1 AND user_id=$2 AND is_active=TRUE""",
        key_id, owner_id)
    return res != "UPDATE 0"


async def authenticate(pool, raw: str) -> dict | None:
    """Ключ из запроса → {"key_id", "owner_id", "scopes"} или None.

    Учёт использования (last_used_at, requests_total) — тем же запросом:
    отдельная запись на каждый вызов API удвоила бы нагрузку на базу.
    Сбой базы — None (отказ), а не исключение: вход в API фейлится закрыто.
    """
    raw = (raw or "").strip()
    if not raw or len(raw) > 200:
        return None
    try:
        row = await pool.fetchrow(
            """UPDATE api_keys
               SET last_used_at=now(), requests_total=requests_total+1
               WHERE key_hash=$1 AND is_active=TRUE
               RETURNING id, user_id, COALESCE(scopes, ARRAY['read','write']) AS scopes""",
            hash_key(raw))
    except Exception as e:
        log.warning("api_keys.authenticate: база недоступна: %s", type(e).__name__)
        return None
    if not row:
        return None
    return {"key_id": int(row["id"]), "owner_id": int(row["user_id"]),
            "scopes": list(row["scopes"] or SCOPES)}

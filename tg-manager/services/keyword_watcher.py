"""Перехватчик ключей из внешних чатов — лидогенерация.

Аккаунт-читатель, состоящий в целевом чате/канале, периодически поллит НОВЫЕ
сообщения (курсор last_msg_id) на совпадение с ключевыми словами. Совпадения
(лиды) пишутся в keyword_hits и пересылаются оператору в Telegram.

Защитные свойства (это активность аккаунта, пусть и read-only):
- read-only поллинг (аккаунт лишь читает чат, в котором уже состоит) — низкий риск;
- карантин: аккаунт под недавним серьёзным ограничением не трогаем (fail-open);
- идемпотентность: курсор last_msg_id + UNIQUE(watcher_id,message_id) — одно
  сообщение даёт один лид даже при повторном поллинге/сбое доставки;
- честный счётчик hits_count; per-watcher ошибка не роняет остальные;
- фоновый цикл разносит поллинг по времени (джиттер) — без синхронного «залпа».

Реальное чтение — account_manager.fetch_chat_messages; в тестах подменяется
через параметр fetch (seam), поэтому логику курсора/матча/идемпотентности можно
проверить на живой БД без Telegram.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random

import asyncpg

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 90          # базовый интервал фонового цикла
_MAX_MSGS_PER_POLL = 100       # сколько новых сообщений тянуть за раз
_DELIVER_TEXT_LIMIT = 300      # обрезка текста лида в уведомлении


def normalize_keywords(raw) -> list[str]:
    """Список ключей → нормализованный (нижний регистр, без пустых/дублей)."""
    if isinstance(raw, str):
        parts = raw.replace("\n", ",").split(",")
    else:
        parts = list(raw or [])
    seen: list[str] = []
    for p in parts:
        k = str(p).strip().lower()
        if k and k not in seen:
            seen.append(k)
    return seen


def match_keywords(text: str | None, keywords: list[str]) -> str | None:
    """Первый ключ, входящий в текст (регистронезависимо). Иначе None."""
    if not text or not keywords:
        return None
    low = text.lower()
    for kw in keywords:
        if kw and kw in low:
            return kw
    return None


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def create_watcher(
    pool: asyncpg.Pool, owner_id: int, account_id: int, chat_ref: str,
    keywords, chat_title: str | None = None,
) -> int:
    kws = normalize_keywords(keywords)
    if not chat_ref or not chat_ref.strip():
        raise ValueError("не указан чат для перехвата")
    if not kws:
        raise ValueError("не указано ни одного ключевого слова")
    return await pool.fetchval(
        "INSERT INTO keyword_watchers(owner_id, account_id, chat_ref, chat_title, keywords) "
        "VALUES($1,$2,$3,$4,$5::jsonb) RETURNING id",
        owner_id, int(account_id), chat_ref.strip(), chat_title,
        json.dumps(kws),
    )


async def list_watchers(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM keyword_watchers WHERE owner_id=$1 ORDER BY created_at DESC", owner_id)
    return [dict(r) for r in rows]


async def set_status(pool: asyncpg.Pool, owner_id: int, watcher_id: int, status: str) -> bool:
    if status not in ("active", "paused"):
        raise ValueError("недопустимый статус")
    res = await pool.execute(
        "UPDATE keyword_watchers SET status=$1 WHERE id=$2 AND owner_id=$3",
        status, watcher_id, owner_id)
    return res.endswith("1")


async def delete_watcher(pool: asyncpg.Pool, owner_id: int, watcher_id: int) -> bool:
    res = await pool.execute(
        "DELETE FROM keyword_watchers WHERE id=$1 AND owner_id=$2", watcher_id, owner_id)
    return res.endswith("1")


async def list_hits(pool: asyncpg.Pool, owner_id: int, limit: int = 20) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM keyword_hits WHERE owner_id=$1 ORDER BY caught_at DESC LIMIT $2",
        owner_id, limit)
    return [dict(r) for r in rows]


# ── Поллинг одного watcher'а ─────────────────────────────────────────────────

async def _default_fetch(pool, watcher, min_id, limit):
    """Реальный seam: подключить аккаунт watcher'а и прочитать новые сообщения."""
    from services import account_manager
    acc = await pool.fetchrow(
        "SELECT * FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        watcher["account_id"], watcher["owner_id"])
    if not acc or not acc["session_str"]:
        return []
    return await account_manager.fetch_chat_messages(
        acc["session_str"], watcher["chat_ref"], min_id=min_id,
        limit=limit, _acc=dict(acc))


async def _deliver_hit(bot, owner_id: int, watcher: dict, msg: dict, kw: str) -> bool:
    """Отправить лид оператору. True — доставлено (delivered)."""
    if bot is None:
        return False
    who = msg.get("from_username")
    who_s = f"@{who}" if who else f"id{msg.get('from_user_id') or '?'}"
    text = (msg.get("text") or "")[:_DELIVER_TEXT_LIMIT]
    chat = watcher.get("chat_title") or watcher.get("chat_ref")
    try:
        await bot.send_message(
            owner_id,
            f"🎯 <b>Лид по ключу</b> «{kw}»\n"
            f"💬 {chat}\n👤 {who_s}\n\n{text}",
            parse_mode="HTML",
        )
        return True
    except Exception as e:
        log.warning("keyword_watcher deliver failed owner=%s: %s", owner_id, e)
        return False


async def poll_watcher(pool: asyncpg.Pool, bot, watcher: dict, *, fetch=None) -> dict:
    """Опросить один watcher: новые сообщения → матч → лид (идемпотентно) → доставка.

    Возвращает {'messages': N, 'hits': M, 'skipped': bool}.
    """
    from services import infra_memory as _infra_mem

    owner_id = watcher["owner_id"]
    wid = watcher["id"]
    # Карантин аккаунта-читателя — не трогаем (fail-open внутри is_account_quarantined).
    if await _infra_mem.is_account_quarantined(pool, watcher["account_id"]):
        await pool.execute(
            "UPDATE keyword_watchers SET last_checked_at=now(), "
            "last_error='account quarantined' WHERE id=$1", wid)
        return {"messages": 0, "hits": 0, "skipped": True}

    keywords = watcher["keywords"]
    if isinstance(keywords, str):
        keywords = json.loads(keywords or "[]")
    cursor = int(watcher.get("last_msg_id") or 0)

    fetch = fetch or _default_fetch
    try:
        messages = await fetch(pool, watcher, cursor, _MAX_MSGS_PER_POLL)
    except Exception as e:
        log.warning("keyword_watcher poll fetch failed wid=%s: %s", wid, e)
        await pool.execute(
            "UPDATE keyword_watchers SET last_checked_at=now(), last_error=$2 WHERE id=$1",
            wid, str(e)[:200])
        return {"messages": 0, "hits": 0, "skipped": False}

    new_hits = 0
    max_id = cursor
    for m in messages:
        mid = int(m.get("message_id") or 0)
        if mid > max_id:
            max_id = mid
        kw = match_keywords(m.get("text"), keywords)
        if not kw:
            continue
        # Идемпотентность: ON CONFLICT(watcher_id,message_id) DO NOTHING.
        row = await pool.fetchrow(
            "INSERT INTO keyword_hits(watcher_id, owner_id, chat_ref, message_id, "
            "from_user_id, from_username, matched_keyword, text) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8) "
            "ON CONFLICT (watcher_id, message_id) DO NOTHING RETURNING id",
            wid, owner_id, watcher.get("chat_ref"), mid,
            m.get("from_user_id"), m.get("from_username"), kw,
            (m.get("text") or "")[:1000])
        if row is None:
            continue  # уже ловили это сообщение — не дубль, не шлём повторно
        new_hits += 1
        delivered = await _deliver_hit(bot, owner_id, watcher, m, kw)
        if delivered:
            await pool.execute(
                "UPDATE keyword_hits SET delivered=TRUE WHERE id=$1", row["id"])

    # Курсор двигаем всегда (даже без совпадений) — иначе перечитываем те же.
    await pool.execute(
        "UPDATE keyword_watchers SET last_msg_id=$2, hits_count=hits_count+$3, "
        "last_checked_at=now(), last_error=NULL WHERE id=$1",
        wid, max_id, new_hits)
    return {"messages": len(messages), "hits": new_hits, "skipped": False}


# ── Фоновый цикл ─────────────────────────────────────────────────────────────

async def run(pool: asyncpg.Pool, bot) -> None:
    """Фоновый цикл: периодически опрашивает активные watcher'ы с джиттером."""
    log.info("keyword_watcher: старт фонового цикла")
    while True:
        try:
            watchers = await pool.fetch(
                "SELECT * FROM keyword_watchers WHERE status='active' "
                "ORDER BY COALESCE(last_checked_at, 'epoch'::timestamptz) ASC LIMIT 50")
            for w in watchers:
                try:
                    await poll_watcher(pool, bot, dict(w))
                except Exception:
                    log.exception("keyword_watcher: poll wid=%s упал", w["id"])
                # Разнос по времени — не «залпом» всеми аккаунтами разом (анти-детект).
                await asyncio.sleep(random.uniform(2.0, 6.0))
        except Exception:
            log.exception("keyword_watcher: цикл упал, продолжаем")
        await asyncio.sleep(_POLL_INTERVAL_S * random.uniform(0.8, 1.3))

"""Сенсор намерений: входящее ЛС в Хранилище → CRM-стадия + тег + алерт.

Замыкает шов «Vault → CRM»: хранилище перестаёт быть архивом и становится
триггером на граф. Оператор задаёт правила «фраза → действие» (напр. «цена» →
стадия proposal + тег «горячий» + уведомление). Сенсор ловит их во ВХОДЯЩИХ
бизнес-сообщениях, находит/создаёт контакт по telegram_user_id, двигает стадию,
вешает тег и шлёт алерт — только когда что-то реально изменилось (без спама).

Пуст по умолчанию (никакого скрытого авто-тегирования); рекомендованный набор
включается в один тап (seed_default_rules).
"""
from __future__ import annotations

import datetime as dt
import logging

import asyncpg

log = logging.getLogger(__name__)

VALID_STAGES = ("lead", "contact", "proposal", "negotiation", "won", "lost")

# Рекомендованный набор (включается опционально). Фразы — в нижнем регистре.
DEFAULT_RULES = [
    {"phrase": "цена", "stage": "proposal", "tag": "интерес-цена", "notify": True},
    {"phrase": "сколько стоит", "stage": "proposal", "tag": "интерес-цена", "notify": True},
    {"phrase": "купить", "stage": "negotiation", "tag": "горячий", "notify": True},
    {"phrase": "оплат", "stage": "negotiation", "tag": "горячий", "notify": True},
    {"phrase": "готов", "stage": "negotiation", "tag": "горячий", "notify": True},
    {"phrase": "не интересно", "stage": "lost", "tag": "отказ", "notify": False},
    {"phrase": "спам", "stage": "lost", "tag": "жалоба", "notify": True},
]

_STAGE_RANK = {s: i for i, s in enumerate(VALID_STAGES)}


async def list_rules(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        "SELECT id, phrase, stage, tag, notify, is_active, hits FROM vault_intent_rules "
        "WHERE owner_id=$1 ORDER BY id", owner_id)
    return [dict(r) for r in rows]


async def add_rule(pool: asyncpg.Pool, owner_id: int, phrase: str,
                   stage: str | None, tag: str | None, notify: bool = True) -> int:
    phrase = (phrase or "").strip().lower()[:120]
    if not phrase:
        raise ValueError("Пустая фраза")
    stage = stage if stage in VALID_STAGES else None
    tag = (tag or "").strip()[:60] or None
    if not stage and not tag:
        raise ValueError("Правило без действия: укажите стадию или тег")
    return int(await pool.fetchval(
        "INSERT INTO vault_intent_rules(owner_id, phrase, stage, tag, notify) "
        "VALUES($1,$2,$3,$4,$5) RETURNING id",
        owner_id, phrase, stage, tag, bool(notify)))


async def delete_rule(pool: asyncpg.Pool, owner_id: int, rule_id: int) -> bool:
    res = await pool.execute(
        "DELETE FROM vault_intent_rules WHERE id=$1 AND owner_id=$2", rule_id, owner_id)
    return not str(res).endswith(" 0")


async def toggle_rule(pool: asyncpg.Pool, owner_id: int, rule_id: int) -> None:
    await pool.execute(
        "UPDATE vault_intent_rules SET is_active = NOT is_active "
        "WHERE id=$1 AND owner_id=$2", rule_id, owner_id)


async def seed_default_rules(pool: asyncpg.Pool, owner_id: int) -> int:
    """Включить рекомендованный набор. Идемпотентно: не плодит дубли по фразе."""
    existing = {r["phrase"] for r in await pool.fetch(
        "SELECT phrase FROM vault_intent_rules WHERE owner_id=$1", owner_id)}
    added = 0
    for r in DEFAULT_RULES:
        if r["phrase"] in existing:
            continue
        await pool.execute(
            "INSERT INTO vault_intent_rules(owner_id, phrase, stage, tag, notify) "
            "VALUES($1,$2,$3,$4,$5)",
            owner_id, r["phrase"], r["stage"], r["tag"], r["notify"])
        added += 1
    return added


async def _ensure_contact(pool: asyncpg.Pool, owner_id: int, peer: dict) -> str | None:
    """Найти контакт по telegram_user_id или создать. Возвращает РЕАЛЬНЫЙ id
    (upsert_contact на конфликте отдаёт новый uuid — нам нужен существующий)."""
    pid = peer.get("peer_user_id")
    if not pid:
        return None
    row = await pool.fetchrow(
        "SELECT id FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2",
        owner_id, pid)
    if row:
        return row["id"]
    from services.contacts_hub.repository import upsert_contact
    await upsert_contact(pool, owner_id, {
        "telegram_user_id": pid, "username": peer.get("peer_username"),
        "first_name": peer.get("peer_name"), "display_name": peer.get("peer_name"),
        "discovered_at": dt.datetime.now(dt.timezone.utc),
    })
    row = await pool.fetchrow(
        "SELECT id FROM unified_contacts WHERE owner_id=$1 AND telegram_user_id=$2",
        owner_id, pid)
    return row["id"] if row else None


def _match(text: str, rules: list[dict]) -> list[dict]:
    low = (text or "").lower()
    return [r for r in rules if r["phrase"] and r["phrase"] in low]


async def scan_incoming(pool: asyncpg.Pool, bot, owner_id: int, peer: dict,
                        text: str, notifier=None) -> dict:
    """Обработать одно ВХОДЯЩЕЕ сообщение. Возвращает {matched, stage, tags,
    notified} (для тестов). notifier — seam отправки алерта (по умолчанию bot).
    Тихо ничего не делает, если правил нет или совпадений нет."""
    if not text:
        return {"matched": 0}
    rules = await pool.fetch(
        "SELECT id, phrase, stage, tag, notify FROM vault_intent_rules "
        "WHERE owner_id=$1 AND is_active=TRUE", owner_id)
    rules = [dict(r) for r in rules]
    matched = _match(text, rules)
    if not matched:
        return {"matched": 0}

    contact_id = await _ensure_contact(pool, owner_id, peer)
    if not contact_id:
        return {"matched": len(matched), "error": "no_contact"}

    # Целевая стадия — самая «продвинутая» среди совпавших (proposal > lead).
    stages = [m["stage"] for m in matched if m["stage"] in _STAGE_RANK]
    target_stage = max(stages, key=lambda s: _STAGE_RANK[s]) if stages else None
    new_tags = [m["tag"] for m in matched if m["tag"]]
    want_notify = any(m["notify"] for m in matched)

    # Текущее состояние — чтобы менять и уведомлять только по факту изменения.
    cur = await pool.fetchrow(
        "SELECT tags FROM unified_contacts WHERE id=$1", contact_id)
    cur_tags = set(cur["tags"] or []) if cur else set()
    cur_stage = await pool.fetchval(
        "SELECT stage FROM contact_crm WHERE owner_id=$1 AND contact_id=$2",
        owner_id, contact_id)

    added_tags = [t for t in dict.fromkeys(new_tags) if t not in cur_tags]
    stage_changed = bool(target_stage) and target_stage != cur_stage

    if added_tags:
        await pool.execute(
            "UPDATE unified_contacts SET tags = ARRAY(SELECT DISTINCT unnest("
            "COALESCE(tags,'{}'::text[]) || $2::text[])), updated_at=NOW() "
            "WHERE id=$1 AND owner_id=$3", contact_id, added_tags, owner_id)
    if stage_changed:
        from services.contacts_hub import crm_engine
        await crm_engine.upsert_crm(pool, owner_id, contact_id, {
            "stage": target_stage,
            "last_interaction_type": "intent",
            "last_message_preview": text[:200],
            "last_interaction_at": dt.datetime.now(dt.timezone.utc),
        })
    # Счётчик срабатываний
    await pool.execute(
        "UPDATE vault_intent_rules SET hits = hits + 1 WHERE id = ANY($1::bigint[])",
        [m["id"] for m in matched])

    notified = False
    if want_notify and (added_tags or stage_changed):
        who = peer.get("peer_name") or peer.get("peer_username") or "Собеседник"
        parts = []
        if stage_changed:
            parts.append(f"стадия → <b>{_stage_label(target_stage)}</b>")
        if added_tags:
            parts.append("теги: " + ", ".join(f"<b>{t}</b>" for t in added_tags))
        msg = (f"🎯 <b>Намерение: {_esc(who)}</b>\n"
               f"«{_esc(text[:160])}»\n" + " · ".join(parts))
        try:
            if notifier:
                await notifier(owner_id, msg)
            else:
                chat_id = await _notify_chat(pool, owner_id)
                if chat_id:
                    await bot.send_message(chat_id, msg, parse_mode="HTML")
            notified = True
        except Exception:
            log.debug("intent_sensor: notify failed owner=%s", owner_id)

    return {"matched": len(matched), "stage": target_stage if stage_changed else None,
            "tags": added_tags, "notified": notified, "contact_id": contact_id}


async def _notify_chat(pool: asyncpg.Pool, owner_id: int):
    row = await pool.fetchrow(
        "SELECT user_chat_id FROM business_connections WHERE owner_id=$1 "
        "AND is_enabled=TRUE ORDER BY updated_at DESC LIMIT 1", owner_id)
    return (row["user_chat_id"] if row else None) or owner_id


def _stage_label(s: str) -> str:
    return {"lead": "Лид", "contact": "Контакт", "proposal": "Предложение",
            "negotiation": "Переговоры", "won": "Выиграно", "lost": "Проиграно"}.get(s, s)


def _esc(t: str) -> str:
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

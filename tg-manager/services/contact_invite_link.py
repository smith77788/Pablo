"""Связка CRM-контакта (unified_contacts) с историей инвайтов и opt-out.

Первопричина: `invite_target_log` (op_worker.py) и `contact_opt_out.py` хранят
инвайты и отказы как строки-target'ы (@username / id / +телефон), полностью
ИЗОЛИРОВАННО от CRM. Карточка контакта в `unified_contacts` не показывает
«приглашался N раз, в группы X, Y» — эта информация технически существует в
БД, но её негде увидеть рядом с самим контактом.

Решение — не новая таблица, а РЕЗОЛВЕР: у контакта есть username/phones/
telegram_user_id, из них считаются те же канонические target-строки, что уже
пишет mass_inviter_engine (через contact_opt_out.normalize_target — тот же
формат, что и в invite_target_log/contact_opt_out, единый источник правды для
нормализации). Дальше — обычный `target = ANY(...)` без новых индексов/полей.

Однонаправленно и READ-ONLY по дизайну: не пишет по строке в contact_history
на каждый инвайт (для крупных кампаний это была бы существенная лишняя
нагрузка на запись) — история считается по требованию, при открытии карточки.
"""
from __future__ import annotations

from typing import Any, Optional

from services.contact_opt_out import normalize_target


def contact_target_candidates(contact: dict) -> list[str]:
    """Все канонические target-формы, под которыми этот контакт мог попасть в
    invite_target_log/contact_opt_out. Порядок: id, username, телефоны."""
    out: list[str] = []
    uid = contact.get("telegram_user_id")
    if uid:
        out.append(str(int(uid)))
    uname = contact.get("username")
    if uname:
        t = normalize_target(str(uname))
        if t:
            out.append(t)
    for p in (contact.get("phones") or []):
        if not p:  # None/"" в JSONB-массиве иначе str()'ится в фантомный target
            continue
        t = normalize_target(str(p))
        if t:
            out.append(t)
    return list(dict.fromkeys(out))  # без дублей, порядок сохранён


async def invite_history_for_contact(
    pool, owner_id: int, contact_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Кому/куда/когда пытались пригласить этот контакт (по всем группам).

    Fail-soft: контакт не найден или у него нет ни одного target'а — пустой
    список, а не ошибка (карточка контакта не должна падать из-за этого)."""
    from services.contacts_hub.repository import get_contact

    data = await get_contact(pool, contact_id, owner_id)
    if not data or not data.get("contact"):
        return []
    candidates = contact_target_candidates(data["contact"])
    if not candidates:
        return []
    await pool.execute(
        "CREATE TABLE IF NOT EXISTS invite_target_log("
        "owner_id BIGINT NOT NULL, group_key TEXT NOT NULL, target TEXT NOT NULL, "
        "op_id BIGINT, created_at TIMESTAMPTZ DEFAULT now(), "
        "PRIMARY KEY(owner_id, group_key, target))"
    )
    # LOWER() на обеих сторонах: invite_target_log.target хранит username в
    # ИСХОДНОМ регистре (mass_inviter_engine.parse_user_refs его не меняет),
    # а candidates — уже канонизированы normalize_target (нижний регистр).
    # Точное сравнение тихо не находило бы историю для username другого
    # регистра — тот же класс бага, что уже закрыт в contact_opt_out.filter_targets.
    rows = await pool.fetch(
        """SELECT group_key, op_id, created_at FROM invite_target_log
           WHERE owner_id=$1 AND LOWER(target) = ANY($2::text[])
           ORDER BY created_at DESC LIMIT $3""",
        owner_id, candidates, limit,
    )
    return [dict(r) for r in (rows or [])]


async def is_contact_opted_out(pool, owner_id: int, contact_id: str) -> bool:
    from services.contacts_hub.repository import get_contact
    from services import contact_opt_out as coo

    data = await get_contact(pool, contact_id, owner_id)
    if not data or not data.get("contact"):
        return False
    opted = await coo.load_opted_out(pool, owner_id)
    if not opted:
        return False
    candidates = contact_target_candidates(data["contact"])
    return any(t in opted for t in candidates)


async def opt_out_contact(
    pool, owner_id: int, contact_id: str, *, reason: Optional[str] = None
) -> list[str]:
    """Пометить контакт «не приглашать» без ручного ввода target'а оператором.

    Регистрирует ВСЕ формы контакта разом (id + username + телефоны), не
    только первую: контакт может иметь и id, и username, а конкретная
    аудитория будущей кампании (сегмент/парсер/CRM-фильтр) может ссылаться на
    него ЛЮБОЙ из этих форм. Opt-out только по одной форме давал бы ложное
    чувство защиты — оператор видит успех, а инвайт всё равно проходит под
    другим идентификатором того же человека (поймано на реальном PG: opt-out
    по telegram_user_id не блокировал аудиторию, ссылавшуюся на username).
    Возвращает список реально записанных target'ов (пусто — контакту нечем
    приглашаться: ни username, ни id, ни телефона)."""
    from services.contacts_hub.repository import get_contact
    from services import contact_opt_out as coo

    data = await get_contact(pool, contact_id, owner_id)
    if not data or not data.get("contact"):
        return []
    candidates = contact_target_candidates(data["contact"])
    stored = []
    for target in candidates:
        got = await coo.add(pool, owner_id, target, reason=reason, source="crm_card")
        if got:
            stored.append(got)
    return stored


async def allow_contact_invite(pool, owner_id: int, contact_id: str) -> bool:
    """Снять opt-out со всех target-форм этого контакта разом (человек мог
    быть добавлен под разными формами — username, потом телефоном)."""
    from services.contacts_hub.repository import get_contact
    from services import contact_opt_out as coo

    data = await get_contact(pool, contact_id, owner_id)
    if not data or not data.get("contact"):
        return False
    removed = False
    for t in contact_target_candidates(data["contact"]):
        if await coo.remove(pool, owner_id, t):
            removed = True
    return removed

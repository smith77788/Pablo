"""Мост из старой базы «CRM → Клиенты» (`crm_contacts`) в «Контакты».

Две базы людей сводятся в одну — `unified_contacts` (план одобрен владельцем
08.11.2026). Пока старая таблица жива, каждый клиент, заведённый в неё, тут же
появляется и в «Контактах»: так сделки, сегменты и рассылки по контактам видят
его без ожидания переноса. Этот же модуль — единственное место, где решается,
кто в «Контактах» «тот же человек», что строка `crm_contacts`.

ПРАВИЛА СЛИЯНИЯ (те же для живой записи и для переноса старых строк):
  * человек ищется по метке `custom_fields.legacy_crm_id`, затем по настоящему
    Telegram ID, затем по @username, затем по телефону;
  * найден — пустые поля дополняются, заполненные не перезаписываются, теги
    объединяются, заметка дописывается в конец, если её там ещё нет;
  * не найден — заводится новый контакт с меткой `legacy_crm_created`, по
    которой перенос можно откатить одним запросом;
  * исключение из рассылок и отказ от них не трогаются никогда.

ПРИДУМАННЫЙ ID. Клиенту, добавленному вручную без Telegram ID, старая форма
выдумывает случайный ID (2–9 млрд). Он неотличим от настоящего, поэтому в
`telegram_user_id` не пишется: его хранит `custom_fields.legacy_tg_user_id`.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def _clean(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


async def mirror_crm_contact(
        pool, owner_id: int, *, legacy_id: int, tg_user_id=None,
        tg_is_real: bool = False, username=None, first_name=None,
        last_name=None, phone=None, tags=None, notes=None) -> str | None:
    """Завести или дополнить контакт для строки `crm_contacts`. Fail-open.

    Возвращает id контакта или None при сбое: запись в старую базу уже прошла,
    и ломать её из-за «Контактов» нельзя.
    """
    if not pool or not owner_id or not legacy_id:
        return None
    username = (_clean(username) or "").lstrip("@") or None
    first_name, last_name = _clean(first_name), _clean(last_name)
    phone, notes = _clean(phone), _clean(notes)
    tags = [t for t in (_clean(t) for t in (tags or [])) if t]
    real_tg = int(tg_user_id) if (tg_is_real and tg_user_id) else None
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                cid = await _find(conn, int(owner_id), int(legacy_id),
                                  real_tg, username, phone)
                if cid:
                    await conn.execute(
                        """UPDATE unified_contacts SET
                             first_name = COALESCE(NULLIF(first_name, ''), $3),
                             last_name  = COALESCE(NULLIF(last_name, ''), $4),
                             username   = COALESCE(NULLIF(username, ''), $5),
                             telegram_user_id = COALESCE(telegram_user_id, $6),
                             phones = CASE
                               WHEN $7::text IS NULL OR COALESCE(phones, '[]'::jsonb) ? $7::text
                                 THEN phones
                               ELSE COALESCE(phones, '[]'::jsonb) || to_jsonb($7::text) END,
                             tags = ARRAY(SELECT DISTINCT t FROM unnest(
                               COALESCE(tags, '{}') || $8::text[]) AS t ORDER BY t),
                             notes = CASE
                               WHEN $9::text IS NULL
                                 OR strpos(COALESCE(notes, ''), $9::text) > 0 THEN notes
                               ELSE concat_ws(E'\\n', NULLIF(notes, ''), $9::text) END,
                             custom_fields = COALESCE(custom_fields, '{}'::jsonb)
                               || jsonb_build_object('legacy_crm_id', $2::bigint),
                             updated_at = NOW()
                           WHERE id = $1""",
                        cid, int(legacy_id), first_name, last_name, username,
                        real_tg, phone, tags, notes)
                    return str(cid)
                extra = {"legacy_crm_id": int(legacy_id), "legacy_crm_created": True}
                if tg_user_id and not real_tg:
                    extra["legacy_tg_user_id"] = int(tg_user_id)
                display = " ".join(p for p in (first_name, last_name) if p) or username
                new_id = await conn.fetchval(
                    """INSERT INTO unified_contacts
                         (owner_id, telegram_user_id, username, first_name, last_name,
                          display_name, phones, tags, notes, custom_fields, discovered_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::text[], $9,
                               $10::jsonb, NOW())
                       RETURNING id""",
                    int(owner_id), real_tg, username, first_name, last_name,
                    display, json.dumps([phone] if phone else []), tags,
                    notes or "", json.dumps(extra))
                return str(new_id)
    except Exception:
        log.warning("mirror_crm_contact owner=%s legacy=%s", owner_id, legacy_id,
                    exc_info=True)
        return None


async def _find(conn, owner_id: int, legacy_id: int, real_tg, username, phone):
    row = await conn.fetchrow(
        "SELECT id FROM unified_contacts WHERE owner_id=$1 "
        "AND custom_fields->>'legacy_crm_id' = $2::text LIMIT 1",
        owner_id, str(legacy_id))
    if not row and real_tg:
        row = await conn.fetchrow(
            "SELECT id FROM unified_contacts WHERE owner_id=$1 "
            "AND telegram_user_id=$2 ORDER BY created_at LIMIT 1", owner_id, real_tg)
    if not row and username:
        row = await conn.fetchrow(
            "SELECT id FROM unified_contacts WHERE owner_id=$1 "
            "AND lower(username)=lower($2) ORDER BY created_at LIMIT 1",
            owner_id, username)
    if not row and phone:
        row = await conn.fetchrow(
            "SELECT id FROM unified_contacts WHERE owner_id=$1 "
            "AND COALESCE(phones, '[]'::jsonb) ? $2 ORDER BY created_at LIMIT 1",
            owner_id, phone)
    return row["id"] if row else None

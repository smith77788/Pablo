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
            res = await _mirror_on(conn, int(owner_id), int(legacy_id), tg_user_id,
                                   real_tg, username, first_name, last_name,
                                   phone, tags, notes)
            return res[0]
    except Exception:
        log.warning("mirror_crm_contact owner=%s legacy=%s", owner_id, legacy_id,
                    exc_info=True)
        return None


async def _mirror_on(conn, owner_id, legacy_id, tg_user_id, real_tg, username,
                     first_name, last_name, phone, tags, notes) -> tuple[str, bool]:
    """Тело зеркала на взятом соединении: (id контакта, создан ли новый)."""
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
            return str(cid), False
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
        return str(new_id), True


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


async def backfill_legacy_crm(pool, *, dry_run: bool = False) -> dict[str, int]:
    """Перенести все строки `crm_contacts` в «Контакты» (шаг 2). Идемпотентно.

    Повторный прогон находит каждого по метке `legacy_crm_id` и ничего не
    плодит. `dry_run=True` делает всё то же в транзакции и откатывает её:
    счётчики честные (учтены и совпадения внутри самого переноса), а база не
    меняется. Старая таблица не меняется никогда.

    Telegram ID строки считается настоящим, если строка пришла не ручным
    вводом (source <> 'manual') или этот ID уже встречается у владельца в
    «Контактах» или среди подписчиков его ботов. Иначе он мог быть придуман.
    """
    out = {"total": 0, "matched": 0, "created": 0, "failed": 0}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT c.id, c.owner_id, c.tg_user_id, c.username, c.first_name,
                      c.last_name, c.phone, c.tags, c.notes,
                      (c.source IS DISTINCT FROM 'manual'
                       OR EXISTS (SELECT 1 FROM unified_contacts u
                                  WHERE u.owner_id=c.owner_id
                                    AND u.telegram_user_id=c.tg_user_id)
                       OR EXISTS (SELECT 1 FROM bot_users b
                                  JOIN managed_bots m ON m.bot_id=b.bot_id
                                  WHERE m.added_by=c.owner_id
                                    AND b.user_id=c.tg_user_id)) AS tg_real
                 FROM crm_contacts c ORDER BY c.id""")
        tx = conn.transaction()
        await tx.start()
        # Два веб-процесса на старте не переносят одно и то же вперегонки:
        # второй видит занятый замок и выходит, ничего не тронув.
        if not await conn.fetchval(
                "SELECT pg_try_advisory_xact_lock(hashtext('legacy_crm_backfill'))"):
            await tx.rollback()
            return {**out, "busy": 1}
        try:
            for r in rows:
                out["total"] += 1
                real = int(r["tg_user_id"]) if (r["tg_real"] and r["tg_user_id"]) else None
                try:
                    _cid, created = await _mirror_on(
                        conn, int(r["owner_id"]), int(r["id"]), r["tg_user_id"], real,
                        (_clean(r["username"]) or "").lstrip("@") or None,
                        _clean(r["first_name"]), _clean(r["last_name"]),
                        _clean(r["phone"]),
                        [t for t in (_clean(t) for t in (r["tags"] or [])) if t],
                        _clean(r["notes"]))
                    out["created" if created else "matched"] += 1
                except Exception:
                    out["failed"] += 1
                    log.warning("backfill_legacy_crm legacy=%s", r["id"], exc_info=True)
        except BaseException:
            await tx.rollback()
            raise
        if dry_run:
            await tx.rollback()
        else:
            await tx.commit()
    log.info("Перенос «Клиентов» в «Контакты»%s: %s",
             " (пробный, откатан)" if dry_run else "", out)
    return out


BACKFILL_KEY = "backfill:legacy_crm_v1"


async def run_backfill_once(pool) -> dict[str, int] | None:
    """Перенос на старте — один раз за жизнь базы (решение владельца 09.11.2026).

    Отметка лежит в журнале `schema_migrations`, тем же способом, что и
    инлайн-миграции. Отметка ставится, только если ни одна строка не упала:
    иначе следующий старт повторит перенос (он идемпотентен). Fail-open: сбой
    переноса не мешает приложению подняться.
    """
    try:
        done = await pool.fetchval(
            "SELECT 1 FROM schema_migrations WHERE filename=$1 AND status='ok'",
            BACKFILL_KEY)
        if done:
            return None
        res = await backfill_legacy_crm(pool)
        if not res.get("failed") and not res.get("busy"):
            await pool.execute(
                """INSERT INTO schema_migrations(filename, status, error_count, applied_at)
                   VALUES($1, 'ok', 0, now())
                   ON CONFLICT (filename) DO UPDATE SET status='ok',
                       error_count=0, applied_at=now()""", BACKFILL_KEY)
        return res
    except Exception:
        log.warning("перенос «Клиентов» в «Контакты» не прошёл, повторится "
                    "на следующем старте", exc_info=True)
        return None


# Аудитория «CRM» (старый тип рассылки и инвайта 'crm') — ЛЮДИ ИЗ «КЛИЕНТОВ»
# внутри «Контактов»: у кого есть метка legacy_crm_id. Подзапрос отдаёт те же
# колонки, что старая таблица (tg_user_id, username, first_name, last_name,
# phone), поэтому читатели сменили только источник. Отличия от старой таблицы:
#   * придуманный ID ручного клиента сюда не попадает — писать на него было
#     некому, рассылка тратила на него попытку;
#   * исключённые из рассылок (excluded) не попадают: старая таблица о них не
#     знала, и личный контакт получал рассылку «по CRM».
# Шаг 4 сведения двух баз (09.11.2026). Подставлять как `FROM {CRM_ROWS} c`.
CRM_ROWS = (
    "(SELECT id, owner_id, telegram_user_id AS tg_user_id, username, first_name, "
    "last_name, phones->>0 AS phone, created_at FROM unified_contacts "
    "WHERE custom_fields ? 'legacy_crm_id' AND excluded = FALSE)"
)

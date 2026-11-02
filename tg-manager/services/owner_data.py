"""Выгрузка и удаление данных владельца по его запросу.

ЗАЧЕМ. Продукт хранит персональные данные третьих лиц (собранные аудитории,
контакты, переписку) и самих владельцев. 152-ФЗ и GDPR требуют двух вещей,
которых не было: отдать человеку его данные по запросу и удалить их по
запросу. Раньше и то и другое делалось только руками в базе.

КАК НАХОДИМ ДАННЫЕ. Список таблиц не зашит в код: схема растёт, и зашитый
список отстал бы с первой же миграцией. Из information_schema берутся три
вида таблиц (`scoped_tables`): с колонкой owner_id; с владельцем в другой
колонке (_OWNER_COLUMN — боты по added_by, API-ключи по user_id…); и строки
ботов владельца по bot_id. Плюс platform_users по user_id.

Первая версия брала ТОЛЬКО owner_id и молча пропускала ботов с токенами и
их аудиторию (managed_bots записан по added_by), отложенные рассылки и
личные API-ключи — владелец читал «Ваши данные стёрты», а ключ продолжал
пускать в REST API.

ЧТО НЕ УДАЛЯЕТСЯ. Платежи, подписки, триалы и комиссии (_RETAINED) — их хранения требует учёт, а
admin_audit_log — след действий админов. Они остаются; профиль в
platform_users обезличивается, а не удаляется, чтобы эти строки не повисли.

СЕКРЕТЫ В ВЫГРУЗКЕ. Значения с префиксом ENC: (строки сессий, токены, прокси)
в выгрузку не попадают: файл уходит в Telegram-чат, и строка сессии в нём —
это ключ от аккаунта. Вместо значения пишется "<зашифровано>".
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import date, datetime
from decimal import Decimal

log = logging.getLogger(__name__)

# Хранятся по закону об учёте или как след админ-действий — не удаляем.
# subscription_trials — защита от повторного триала, commission_ledger —
# комиссии платформы (учёт).
_RETAINED = frozenset({
    "payments", "subscriptions", "subscription_trials", "commission_ledger",
    "admin_audit_log", "schema_migrations",
})

_MAX_DELETE_PASSES = 6

# Таблицы, где владелец записан НЕ в owner_id. Колонка проверена по смыслу
# каждой таблицы: это id владельца, а не человека из аудитории. В bot_users,
# user_tags, relay_sessions и подобных `user_id` — подписчик бота; такие
# таблицы находятся по bot_id ботов владельца (ниже), а не по этой колонке.
#
# Без этого списка удаление пропускало самое важное: ботов с токенами
# (managed_bots.added_by), а с ними каскадом — их аудитории, рассылки и
# отложенные рассылки; личные API-ключи (api_keys.user_id) продолжали
# пускать в REST API после «Ваши данные стёрты».
_OWNER_COLUMN = {
    "managed_bots": "added_by",
    "api_keys": "user_id",
    "broadcasts": "created_by",
    "scheduled_broadcasts": "created_by",
    "notification_settings": "user_id",
    "notification_dedup": "user_id",
    "notification_held": "user_id",
    "growth_settings": "user_id",
    "growth_schedule": "user_id",
    "workspace_invites": "created_by",
    "platform_admin_sessions": "user_id",
    "error_reports": "user_id",
}

_COLUMNS_SQL = """
SELECT c.table_name, c.column_name
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON t.table_schema = c.table_schema AND t.table_name = c.table_name
 WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
   AND c.column_name IN ('owner_id', 'bot_id', 'added_by', 'user_id', 'created_by')
"""


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def scoped_tables(conn, owner_id: int) -> tuple[list[tuple[str, str, object]], list[int]]:
    """Все таблицы с данными владельца: [(таблица, условие, аргумент)], bot_ids.

    Три вида:
      * колонка owner_id — `owner_id = $1`;
      * владелец в другой колонке (_OWNER_COLUMN) — `<колонка> = $1`;
      * строки ботов владельца — `bot_id = ANY($1)`: у таблиц, где есть
        bot_id и нет owner_id (аудитория, рассылки, воронки бота). bot_id
        уникален в managed_bots, поэтому чужих строк так не задеть.
    """
    cols: dict[str, set[str]] = {}
    for r in await conn.fetch(_COLUMNS_SQL):
        cols.setdefault(r["table_name"], set()).add(r["column_name"])
    oid = int(owner_id)
    out: list[tuple[str, str, object]] = []
    for t in sorted(cols):
        have = cols[t]
        if "owner_id" in have:
            out.append((t, "owner_id = $1", oid))
        elif _OWNER_COLUMN.get(t) in have:
            out.append((t, f"{_qi(_OWNER_COLUMN[t])} = $1", oid))
    bot_ids: list[int] = []
    if "added_by" in cols.get("managed_bots", set()):
        bot_ids = [int(r["bot_id"]) for r in await conn.fetch(
            "SELECT bot_id FROM managed_bots WHERE added_by=$1", oid)]
    if bot_ids:
        taken = {t for t, _, _ in out}
        for t in sorted(cols):
            if t not in taken and "bot_id" in cols[t]:
                out.append((t, "bot_id = ANY($1::bigint[])", bot_ids))
    return out, bot_ids


def _jsonable(v):
    if isinstance(v, str) and v.startswith("ENC:"):
        return "<зашифровано>"
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} байт>"
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


async def export_owner_data(pool, owner_id: int) -> tuple[bytes, dict[str, int]]:
    """ZIP с JSON по каждой таблице, где есть строки владельца.

    Возвращает (байты архива, {таблица: число строк}).
    """
    counts: dict[str, int] = {}
    buf = io.BytesIO()
    async with pool.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            tables, _bots = await scoped_tables(conn, owner_id)
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                prof = await conn.fetch(
                    "SELECT * FROM platform_users WHERE user_id=$1", int(owner_id))
                if prof:
                    zf.writestr("platform_users.json", json.dumps(
                        [_jsonable(dict(r)) for r in prof], ensure_ascii=False, indent=1))
                    counts["platform_users"] = len(prof)
                for t, cond, arg in tables:
                    rows = await conn.fetch(
                        f"SELECT * FROM {_qi(t)} WHERE {cond}", arg)
                    if not rows:
                        continue
                    counts[t] = len(rows)
                    zf.writestr(f"{t}.json", json.dumps(
                        [_jsonable(dict(r)) for r in rows], ensure_ascii=False, indent=1))
                zf.writestr("README.txt", (
                    "Выгрузка ваших данных Infragram.\n"
                    "Один файл JSON на таблицу. Строки сессий, токены и прокси "
                    "заменены на <зашифровано>: файл не должен становиться ключом "
                    "от ваших аккаунтов.\n"))
    return buf.getvalue(), counts


async def delete_owner_data(pool, owner_id: int) -> dict:
    """Удалить данные владельца. Возвращает {deleted: {таблица: n}, failed: [...]}.

    Всё в одной транзакции: либо удалено всё, что удаляется, либо ничего.
    Порядок таблиц из-за внешних ключей заранее неизвестен, поэтому идём
    проходами: таблица, удаление из которой упёрлось в чужой ключ, ждёт
    следующего прохода (под SAVEPOINT, чтобы ошибка не губила транзакцию).
    """
    oid = int(owner_id)
    deleted: dict[str, int] = {}
    async with pool.acquire() as conn:
        async with conn.transaction():
            scoped, _bots = await scoped_tables(conn, oid)
            pending = [x for x in scoped if x[0] not in _RETAINED]
            for _pass in range(_MAX_DELETE_PASSES):
                failed: list[tuple[str, str, object]] = []
                for item in pending:
                    t, cond, arg = item
                    try:
                        async with conn.transaction():
                            status = await conn.execute(
                                f"DELETE FROM {_qi(t)} WHERE {cond}", arg)
                        n = int(status.split()[-1]) if status else 0
                        if n:
                            deleted[t] = deleted.get(t, 0) + n
                    except Exception:
                        failed.append(item)
                if not failed or len(failed) == len(pending):
                    pending = failed
                    break
                pending = failed
            if pending:
                # Не смогли удалить всё — откатываем целиком: половинчатое
                # удаление хуже отказа (данные частично живы, а человек думает,
                # что их нет).
                raise OwnerDataDeleteError([t for t, _, _ in pending])
            # Профиль обезличиваем: платежи и подписки ссылаются на него.
            await conn.execute(
                """UPDATE platform_users
                      SET username=NULL, first_name=NULL, notes=NULL
                    WHERE user_id=$1""", oid)
    log.warning("AUDIT owner_data_deleted owner_id=%s tables=%d rows=%d",
                oid, len(deleted), sum(deleted.values()))
    return {"deleted": deleted}


class OwnerDataDeleteError(RuntimeError):
    def __init__(self, tables: list[str]):
        super().__init__("не удалось удалить данные из: " + ", ".join(tables))
        self.tables = tables

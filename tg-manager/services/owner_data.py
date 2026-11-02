"""Выгрузка и удаление данных владельца по его запросу.

ЗАЧЕМ. Продукт хранит персональные данные третьих лиц (собранные аудитории,
контакты, переписку) и самих владельцев. 152-ФЗ и GDPR требуют двух вещей,
которых не было: отдать человеку его данные по запросу и удалить их по
запросу. Раньше и то и другое делалось только руками в базе.

КАК НАХОДИМ ДАННЫЕ. Список таблиц не зашит в код: схема растёт (200+ таблиц с
owner_id), и зашитый список отстал бы с первой же миграцией. Берём из
information_schema все таблицы public, где есть колонка owner_id, плюс
platform_users по user_id.

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

_TABLES_SQL = """
SELECT c.table_name
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON t.table_schema = c.table_schema AND t.table_name = c.table_name
 WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
   AND c.column_name = 'owner_id'
 ORDER BY c.table_name
"""

_MAX_DELETE_PASSES = 6


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def owner_tables(conn) -> list[str]:
    return [r["table_name"] for r in await conn.fetch(_TABLES_SQL)]


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
            tables = await owner_tables(conn)
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                prof = await conn.fetch(
                    "SELECT * FROM platform_users WHERE user_id=$1", int(owner_id))
                if prof:
                    zf.writestr("platform_users.json", json.dumps(
                        [_jsonable(dict(r)) for r in prof], ensure_ascii=False, indent=1))
                    counts["platform_users"] = len(prof)
                for t in tables:
                    rows = await conn.fetch(
                        f"SELECT * FROM {_qi(t)} WHERE owner_id=$1", int(owner_id))
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
            pending = [t for t in await owner_tables(conn) if t not in _RETAINED]
            for _pass in range(_MAX_DELETE_PASSES):
                failed: list[str] = []
                for t in pending:
                    try:
                        async with conn.transaction():
                            status = await conn.execute(
                                f"DELETE FROM {_qi(t)} WHERE owner_id=$1", oid)
                        n = int(status.split()[-1]) if status else 0
                        if n:
                            deleted[t] = deleted.get(t, 0) + n
                    except Exception:
                        failed.append(t)
                if not failed or len(failed) == len(pending):
                    pending = failed
                    break
                pending = failed
            if pending:
                # Не смогли удалить всё — откатываем целиком: половинчатое
                # удаление хуже отказа (данные частично живы, а человек думает,
                # что их нет).
                raise OwnerDataDeleteError(pending)
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

#!/usr/bin/env python3
"""Перешифровать секреты базы текущим ключом token_vault (ротация ключа).

ЗАЧЕМ. Все секреты продукта (строки сессий, токены ботов, прокси, ключи
SMM-панелей, токены Cloudflare и т. д.) зашифрованы одним ключом
TOKEN_ENCRYPTION_KEY. Сменить его можно без потери данных так:

  1. Сделать бэкап базы.
  2. Новый ключ — в TOKEN_ENCRYPTION_KEY, прежний — в
     TOKEN_ENCRYPTION_KEYS_PREVIOUS (через запятую, если прежних несколько).
     Перезапустить сервис: он читает старые строки прежним ключом, а новые
     пишет новым.
  3. Прогнать этот скрипт без --apply: он покажет, сколько строк в каждой
     колонке ещё на прежнем ключе.
  4. Прогнать с --apply. Скрипт идемпотентен: повторный прогон перешифрует
     только то, что осталось.
  5. Когда dry-run показывает ноль, убрать прежний ключ из
     TOKEN_ENCRYPTION_KEYS_PREVIOUS. Исключение — файлы облака
     (token_vault.encrypt_bytes): они лежат вне базы, этот скрипт их не видит.

Если TOKEN_ENCRYPTION_KEY раньше не был задан и ключ выводился из токена бота,
то для перехода на отдельный ключ достаточно задать TOKEN_ENCRYPTION_KEY:
ключ из MANAGER_BOT_TOKEN остаётся в связке на чтение сам.

КАК. Скрипт не знает список секретных колонок заранее: список в коде отстаёт от
схемы. Он берёт все текстовые колонки public-схемы, ищет значения с префиксом
"ENC:" и перешифровывает те, что открываются прежним ключом
(token_vault.needs_reencrypt). Строки, которые не открывает ни один ключ, не
трогаются и считаются отдельно: перешифровать их нечем.

Обновление идёт по ctid с проверкой старого значения, так что строку,
изменённую приложением во время прогона, скрипт не перезапишет.

Использование:
  python3 deploy/scripts/rotate_vault_key.py              # dry-run
  python3 deploy/scripts/rotate_vault_key.py --apply
  python3 deploy/scripts/rotate_vault_key.py --apply --only tg_accounts,user_proxies
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_COLUMNS_SQL = """
SELECT c.table_name, c.column_name
  FROM information_schema.columns c
  JOIN information_schema.tables t
    ON t.table_schema = c.table_schema AND t.table_name = c.table_name
 WHERE c.table_schema = 'public'
   AND t.table_type = 'BASE TABLE'
   AND c.data_type IN ('text', 'character varying')
 ORDER BY c.table_name, c.column_name
"""


def _qi(name: str) -> str:
    """Экранировать идентификатор (имена берутся из information_schema)."""
    return '"' + name.replace('"', '""') + '"'


async def rotate_column(conn, table: str, column: str, *, apply: bool,
                        batch_size: int = 500) -> dict:
    """Перешифровать одну колонку. Возвращает счётчики."""
    from services.token_vault import _open_gcm, needs_reencrypt, reencrypt_token
    import base64

    stats = {"table": table, "column": column, "old_key": 0, "rotated": 0,
             "unreadable": 0, "skipped_changed": 0}
    t, c = _qi(table), _qi(column)
    rows = await conn.fetch(
        f"SELECT ctid::text AS rid, {c} AS v FROM {t} WHERE {c} LIKE 'ENC:%'")
    for row in rows:
        value = row["v"]
        if needs_reencrypt(value):
            stats["old_key"] += 1
            if not apply:
                continue
            new_value = reencrypt_token(value)
            status = await conn.execute(
                f"UPDATE {t} SET {c}=$1 WHERE ctid=$2::tid AND {c}=$3",
                new_value, row["rid"], value)
            if status.endswith(" 1"):
                stats["rotated"] += 1
            else:
                stats["skipped_changed"] += 1
            continue
        try:
            _open_gcm(base64.b64decode(value[4:]))
        except Exception:
            stats["unreadable"] += 1
    return stats


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="Перешифровать (по умолчанию только счёт)")
    parser.add_argument("--only", default="",
                        help="Таблицы через запятую; по умолчанию все")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("ERROR: DATABASE_URL не задан.", file=sys.stderr)
        return 1
    if not os.environ.get("TOKEN_ENCRYPTION_KEYS_PREVIOUS") and not (
            os.environ.get("TOKEN_ENCRYPTION_KEY") and os.environ.get("MANAGER_BOT_TOKEN")):
        print("Прежних ключей в связке нет (TOKEN_ENCRYPTION_KEYS_PREVIOUS пуст): "
              "перешифровывать нечего.")
        return 0

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        cols = [(r["table_name"], r["column_name"]) for r in await conn.fetch(_COLUMNS_SQL)]
        cols = [(t, c) for t, c in cols if not only or t in only]
        print(("=== ПРИМЕНЕНИЕ ===" if args.apply else "=== DRY-RUN ===")
              + f" колонок к проверке: {len(cols)}")
        total_old = total_rot = total_bad = 0
        for table, column in cols:
            st = await rotate_column(conn, table, column, apply=args.apply)
            if st["old_key"] or st["unreadable"]:
                print(f"  {table}.{column}: на прежнем ключе {st['old_key']}, "
                      f"перешифровано {st['rotated']}, нечитаемых {st['unreadable']}"
                      + (f", изменились во время прогона {st['skipped_changed']}"
                         if st["skipped_changed"] else ""))
            total_old += st["old_key"]
            total_rot += st["rotated"]
            total_bad += st["unreadable"]
        print(f"Итого: на прежнем ключе {total_old}, перешифровано {total_rot}, "
              f"нечитаемых ни одним ключом {total_bad}.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Сделать baseline схемы: снимок структуры живой базы + манифест файлов истории.

Зачем. Журнал `schema_migrations` избавил обновление от повторного проигрывания
миграций, но чистая база по-прежнему прокатывает всю историю подряд — сейчас это
194 файла. Baseline сворачивает историю в один снимок: новое окружение
поднимается одним файлом, а файлы, которые снимок уже содержит, помечаются
применёнными и больше не выполняются.

Что делает:
  1. `pg_dump --schema-only` с живой (лучше — только что развёрнутой из истории)
     базы, очищенный от настроек сессии (см. `sanitize_dump`) →
     `database/baseline/baseline.sql`;
  2. перечисляет файлы `schema*.sql`, которые снимок покрывает →
     `database/baseline/baseline.manifest`.

ПОЧЕМУ НЕ В КОРНЕ И НЕ `schema_*.sql`. Раннер и полтора десятка тестов-линтеров
собирают историю маской `schema*.sql` (корень и `database/`). Снимок под такой
маской выполнился бы на ПРОДЕ как очередная миграция — поверх живой схемы,
вместе с `set_config('search_path', '', false)` из дампа, после которого все
следующие файлы истории на том же соединении не находят ни одной таблицы.
Каталог `database/baseline/` маска не видит.

Использование:
    python deploy/scripts/make_schema_baseline.py \\
        --database-url postgres://... \\
        [--out-dir .] [--upto schema_v193_channel_ownership.sql]

Снимок снимают с базы, ПОЛНОСТЬЮ прошедшей историю миграций (проще всего — с
чистой базы, поднятой текущим кодом). Снимать с продовой базы можно, но тогда
проверьте, что в дампе нет ничего лишнего: расширений, ролей, данных.

Дальше: закоммитить оба файла, выкатить, убедиться, что новое окружение встаёт
одним снимком, и только после этого убирать покрытые файлы истории из репозитория
(отдельным шагом — см. docs/SCHEMA_CONSOLIDATION_PLAN.md).
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys


def schema_files(root: str) -> list[str]:
    """Файлы схемы в порядке применения — тем же ключом, что и рантайм."""
    paths = glob.glob(os.path.join(root, "schema*.sql")) + glob.glob(
        os.path.join(root, "database", "schema*.sql"))

    # Ключ ровно как у рантайма (`database.db.migration_version_key` +
    # имя при равной версии). Импортировать его нельзя: database.db тянет
    # config, а скрипт запускают в голой среде. Раньше здесь склеивались ВСЕ
    # цифры имени, и schema_v190_geo_country_iso2.sql становился версией 1902
    # — уезжал в конец, и `--upto` резал историю не там.
    def version_key(p: str) -> tuple[int, str]:
        name = os.path.basename(p)
        if name == "schema.sql":
            return (0, name)
        m = re.match(r"schema_v(\d+)", name)
        if m:
            return (int(m.group(1)), name)
        digits = "".join(filter(str.isdigit, name))
        return (int(digits) if digits else 0, name)

    paths.sort(key=version_key)
    seen: set[str] = set()
    ordered: list[str] = []
    for p in paths:
        bn = os.path.basename(p)
        # baseline сам себя не покрывает
        if bn in seen or bn.startswith("schema_baseline"):
            continue
        seen.add(bn)
        ordered.append(p)
    return ordered


# Строки дампа, которые нельзя исполнять через раннер миграций.
#
# `\restrict`/`\unrestrict` — мета-команды psql (pg_dump 16.10+): asyncpg
# отдаёт их серверу как SQL, и первый же оператор снимка падает синтаксисом.
# `SET ...` и `set_config(...)` меняют настройки СОЕДИНЕНИЯ, а снимок и
# хвост истории после него идут по одному соединению: `SET lock_timeout = 0`
# снимал бы потолок ожидания блокировки, который раннер ставит намеренно, а
# пустой search_path ломал бы каждый следующий файл истории.
_SESSION_PREFIXES = ("SET ", "SELECT pg_catalog.set_config(")


def sanitize_dump(sql: str) -> str:
    """Убрать из дампа мета-команды psql и настройки сессии.

    Ничего, кроме этих строк, не трогается: структура схемы остаётся ровно
    той, что снял pg_dump. Строки ищутся только в начале строки — внутри тел
    функций (`$$ ... $$`) они идут с отступом и не задеваются.
    """
    out = []
    for line in sql.splitlines():
        if line.startswith("\\"):
            continue
        if line.startswith(_SESSION_PREFIXES):
            continue
        out.append(line)
    text = "\n".join(out).strip() + "\n"
    # Объекты дамп называет с явной схемой (`public.tg_accounts`), а сама
    # схема public в чистой базе уже есть. Пустых строк подряд не копим.
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


def dump_schema(database_url: str) -> str:
    """Снять структуру базы. Без владельцев и прав — их даёт окружение."""
    # Журнал миграций в снимок не берём: его заводит сам раннер ДО снимка
    # (SCHEMA_MIGRATIONS_DDL), и второй первичный ключ на нём ронял снимок
    # целиком — чистая база молча уходила обычным путём по истории.
    cmd = ["pg_dump", "--schema-only", "--no-owner", "--no-privileges",
           "--no-comments", "--exclude-table=public.schema_migrations",
           database_url]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"pg_dump упал: {out.stderr.strip()[:500]}")
    return sanitize_dump(out.stdout)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="строка подключения (по умолчанию $DATABASE_URL)")
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".."),
        help="корень tg-manager: оттуда берётся история, в его "
             "database/baseline/ кладутся снимок и манифест")
    ap.add_argument("--upto", default=None,
                    help="последний файл истории, который покрывает снимок "
                         "(по умолчанию — все существующие)")
    args = ap.parse_args()

    if not args.database_url:
        sys.exit("нужен --database-url или переменная DATABASE_URL")

    root = os.path.abspath(args.out_dir)
    files = [os.path.basename(p) for p in schema_files(root)]
    if not files:
        sys.exit(f"в {root} не нашлось ни одного schema*.sql")
    if args.upto:
        if args.upto not in files:
            sys.exit(f"--upto {args.upto}: такого файла среди schema*.sql нет")
        files = files[: files.index(args.upto) + 1]

    sql = dump_schema(args.database_url)
    if "CREATE TABLE" not in sql:
        sys.exit("в дампе нет ни одной CREATE TABLE — база пустая? снимок не пишу")

    out_dir = os.path.join(root, "database", "baseline")
    os.makedirs(out_dir, exist_ok=True)
    sql_path = os.path.join(out_dir, "baseline.sql")
    man_path = os.path.join(out_dir, "baseline.manifest")
    with open(sql_path, "w", encoding="utf-8") as f:
        f.write(sql)
    with open(man_path, "w", encoding="utf-8") as f:
        f.write("# Файлы истории миграций, которые содержит baseline.sql.\n")
        f.write("# На чистой базе они помечаются применёнными и не выполняются.\n")
        f.write("# Сгенерировано deploy/scripts/make_schema_baseline.py — правьте осознанно.\n")
        for name in files:
            f.write(name + "\n")

    print(f"snapshot: {sql_path} ({len(sql.splitlines())} строк)")
    print(f"манифест: {man_path} ({len(files)} файлов, последний {files[-1]})")
    print("\nДальше: проверьте, что чистая база поднимается снимком, и только потом "
          "убирайте покрытые файлы истории (docs/SCHEMA_CONSOLIDATION_PLAN.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

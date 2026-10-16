#!/usr/bin/env python3
"""Сделать baseline схемы: снимок структуры живой базы + манифест файлов истории.

Зачем. Журнал `schema_migrations` избавил обновление от повторного проигрывания
миграций, но чистая база по-прежнему прокатывает всю историю подряд — сейчас это
194 файла. Baseline сворачивает историю в один снимок: новое окружение
поднимается одним файлом, а файлы, которые снимок уже содержит, помечаются
применёнными и больше не выполняются.

Что делает:
  1. `pg_dump --schema-only` с живой (лучше — только что развёрнутой из истории)
     базы → `schema_baseline.sql`;
  2. перечисляет файлы `schema*.sql`, которые снимок покрывает →
     `schema_baseline.manifest`.

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
import subprocess
import sys


def schema_files(root: str) -> list[str]:
    """Файлы схемы в порядке применения — тем же ключом, что и рантайм."""
    paths = glob.glob(os.path.join(root, "schema*.sql")) + glob.glob(
        os.path.join(root, "database", "schema*.sql"))

    def version_key(p: str) -> int:
        name = os.path.basename(p)
        if name == "schema.sql":
            return 0
        digits = "".join(filter(str.isdigit, name))
        return int(digits) if digits else 0

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


def dump_schema(database_url: str) -> str:
    """Снять структуру базы. Без владельцев и прав — их даёт окружение."""
    cmd = ["pg_dump", "--schema-only", "--no-owner", "--no-privileges",
           "--no-comments", database_url]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"pg_dump упал: {out.stderr.strip()[:500]}")
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"),
                    help="строка подключения (по умолчанию $DATABASE_URL)")
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".."),
        help="куда класть snapshot и манифест (по умолчанию корень tg-manager)")
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

    sql_path = os.path.join(root, "schema_baseline.sql")
    man_path = os.path.join(root, "schema_baseline.manifest")
    with open(sql_path, "w", encoding="utf-8") as f:
        f.write(sql)
    with open(man_path, "w", encoding="utf-8") as f:
        f.write("# Файлы истории миграций, которые содержит schema_baseline.sql.\n")
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

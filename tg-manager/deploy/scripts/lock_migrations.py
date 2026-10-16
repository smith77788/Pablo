#!/usr/bin/env python3
"""Фиксация контрольных сумм миграций в `schema_checksums.txt`.

Зачем. Раннер миграций ведёт журнал `schema_migrations` и уже применённый файл
второй раз не проигрывает. Значит, правка старого `schema_vN.sql` на прод не
приедет никогда: у работающей базы файл помечен применённым, а у нового
окружения выполнится уже новый текст. Схема прода и схема кода расходятся
молча, и обнаруживается это через недели — по «column does not exist» в
проде при зелёных тестах.

Инструмент вписывает в манифест НОВЫЕ файлы и не трогает записанные: сумму
уже зафиксированного файла он менять отказывается, потому что именно это
изменение и запрещено. Если файл ещё не уезжал в прод и правка осознанная —
строку в манифесте правят руками.

Запуск:
    python deploy/scripts/lock_migrations.py            # вписать новые файлы
    python deploy/scripts/lock_migrations.py --check    # только показать разницу
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

from database.migration_manifest import (  # noqa: E402
    collect_migration_checksums,
    parse_checksum_manifest,
)

MANIFEST = os.path.join(_ROOT, "schema_checksums.txt")

_HEADER = """\
# Контрольные суммы миграций. Проверяет tests/test_migrations_are_frozen.py.
#
# Уже применённый schema_vN.sql править нельзя: прод пропустит его по журналу
# schema_migrations, и схема прода разойдётся со схемой кода. Нужна правка —
# заводится новый файл schema_vN+1.sql (только вперёд, ADD COLUMN).
#
# Добавили миграцию — впишите её сюда:
#     python deploy/scripts/lock_migrations.py
"""


def load_manifest() -> dict[str, str]:
    if not os.path.exists(MANIFEST):
        return {}
    with open(MANIFEST, encoding="utf-8") as f:
        return parse_checksum_manifest(f.read())


def write_manifest(entries: dict[str, str]) -> None:
    lines = [_HEADER]
    for name in sorted(entries):
        lines.append(f"{entries[name]}  {name}\n")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        f.write("".join(lines))


def main() -> int:
    check_only = "--check" in sys.argv
    on_disk = collect_migration_checksums(_ROOT)
    locked = load_manifest()

    new = sorted(n for n in on_disk if n not in locked)
    changed = sorted(n for n, d in locked.items() if n in on_disk and on_disk[n] != d)
    gone = sorted(n for n in locked if n not in on_disk)

    for name in new:
        print(f"новый:   {name}")
    for name in changed:
        print(f"изменён: {name}  ← так делать нельзя, заведите новый schema_vN.sql")
    for name in gone:
        print(f"удалён:  {name}  ← миграцию нельзя удалять")

    if check_only:
        return 1 if (changed or gone or new) else 0

    if changed or gone:
        print(
            "\nМанифест не обновлён: изменённые и удалённые миграции инструмент "
            "не фиксирует. Верните файл или заведите новый schema_vN.sql."
        )
        return 1

    if not new:
        print("Новых миграций нет, манифест не тронут.")
        return 0

    locked.update({n: on_disk[n] for n in new})
    write_manifest(locked)
    print(f"\nВписано новых файлов: {len(new)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Храповик: миграции применяются в порядке своих номеров.

`create_pool` собирает schema_vN.sql и сортирует их ключом
`migration_version_key`. Ключ раньше склеивал ВСЕ цифры имени, поэтому
schema_v190_geo_country_iso2.sql считался 1902-м и применялся ПОСЛЕ v200.
Конкретно тому файлу повезло — идемпотентный бэкфилл данных, порядок ему
безразличен. Но миграция, заводящая таблицу, при таком сдвиге уехала бы
позже той, которая на эту таблицу опирается, и старт падал бы на ровном месте.

Заметить это в обычном тесте нельзя: миграции применяются только при живом
подключении к базе, а имя файла выглядит совершенно нормально.
"""
from __future__ import annotations

import glob
import os
import pathlib
import re

from database.db import migration_version_key

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _migration_files() -> list[str]:
    return sorted(
        glob.glob(str(ROOT / "schema*.sql"))
        + glob.glob(str(ROOT / "database" / "schema*.sql"))
    )


def test_migration_files_exist():
    """Если выборка вдруг пуста, все проверки ниже станут пустыми."""
    assert len(_migration_files()) > 100


def test_sort_key_equals_declared_version():
    """Ключ сортировки обязан совпадать с номером в имени файла."""
    wrong = []
    for path in _migration_files():
        name = os.path.basename(path)
        m = re.match(r"schema_v(\d+)", name)
        if not m:
            continue
        declared = int(m.group(1))
        actual = migration_version_key(path)
        if actual != declared:
            wrong.append(f"{name}: номер {declared}, сортируется как {actual}")
    assert not wrong, (
        "миграции применятся не в том порядке, в каком пронумерованы:\n  "
        + "\n  ".join(wrong)
    )


def test_key_ignores_digits_in_description():
    """Прямая проверка разбора — на именах, которые ломали прежний ключ."""
    assert migration_version_key("schema_v190_geo_country_iso2.sql") == 190
    assert migration_version_key("schema_v205_2fa_backup_codes.sql") == 205
    assert migration_version_key("schema_v201_ipv6_proxies.sql") == 201
    assert migration_version_key("schema_v146_ban_weather.sql") == 146
    assert migration_version_key("schema.sql") == 0


def test_duplicate_numbers_are_distinct_files():
    """Один номер у двух файлов допустим — журнал schema_migrations ключуется
    по имени файла, так что применятся оба. Недопустимо совпадение ИМЁН:
    дедуп по basename тихо выбросил бы вторую миграцию.
    """
    names = [os.path.basename(p) for p in _migration_files()]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, (
        f"одинаковые имена файлов миграций — вторая будет молча пропущена: {sorted(dupes)}"
    )

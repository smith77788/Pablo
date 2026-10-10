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
import random
import pathlib
import re

from database.db import migration_version_key, ordered_migration_files

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


# ── Порядок при одинаковых номерах ────────────────────────────────────────────

def test_order_is_independent_of_filesystem_order():
    """Порядок применения не зависит от того, как файлы легли на диск.

    Девять пар файлов делят номер версии (v146, v185, v193, v200, v201, v206,
    v211, v212, v213). Раньше сортировка шла по одному номеру, а сортировка в
    Python устойчивая — при равных ключах порядок оставался тем, в каком файлы
    вернула файловая система. На поднятой базе это незаметно: журнал
    schema_migrations ключуется по имени файла, применённое не переприменяется.
    Но чистая база прокатывает всю историю с нуля, и порядок DDL получался
    разным на разных машинах.
    """
    files = _migration_files()
    forward = ordered_migration_files(list(files))
    backward = ordered_migration_files(list(reversed(files)))
    assert forward == backward, "порядок миграций зависит от порядка файлов на диске"

    shuffled = list(files)
    random.Random(1234).shuffle(shuffled)
    assert ordered_migration_files(shuffled) == forward


def test_order_never_goes_backwards_in_version():
    """Внутри итогового списка номера версий не убывают."""
    versions = [migration_version_key(p) for p in ordered_migration_files(_migration_files())]
    assert versions == sorted(versions)


def test_duplicate_versions_are_ordered_by_name():
    """У пары с общим номером порядок задаётся именем файла, а не диском."""
    got = ordered_migration_files([
        "/x/schema_v200_discovered_bots.sql",
        "/x/schema_v200_daughter_groups.sql",
        "/x/schema_v199_chatlist_folders.sql",
    ])
    assert [os.path.basename(p) for p in got] == [
        "schema_v199_chatlist_folders.sql",
        "schema_v200_daughter_groups.sql",
        "schema_v200_discovered_bots.sql",
    ]


def test_same_basename_applied_once():
    """Один и тот же файл в корне и в database/ применяется один раз."""
    got = ordered_migration_files([
        "/x/schema_v10.sql",
        "/x/database/schema_v10.sql",
    ])
    assert len(got) == 1


def test_real_files_survive_ordering():
    """Дедуп не должен выбрасывать ничего из настоящего набора."""
    files = _migration_files()
    assert len(ordered_migration_files(files)) == len(files)

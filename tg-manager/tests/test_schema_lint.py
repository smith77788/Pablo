"""Статический lint схем — ловит классы багов, ронявших миграцию на проде.

Без живой БД (в CI её нет), но каждый из этих трёх багов реально был найден и
исправлен, и каждый молча ронял часть миграции (per-statement tolerance), из-за
чего целые фичи оставались без таблиц/индексов:

  1. REFERENCES users(id) — таблицы `users` не существует (каноническая —
     platform_users). Ронял ВСЕ gift_* таблицы (schema_v77).
  2. NOW()/CURRENT_* в предикате CREATE INDEX — функции в предикате должны быть
     IMMUTABLE (schema_v136).
  3. CREATE INDEX ON <table> в файле с номером МЕНЬШЕ, чем файл, создающий эту
     таблицу → "relation does not exist" (schema_v136 vs channel_members в v137).
"""
from __future__ import annotations

import glob
import os
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _version(path: str) -> int:
    name = os.path.basename(path)
    if name == "schema.sql":
        return 0  # базовый файл применяется первым (как в create_pool)
    # Версию берём как рантайм (database/db.py::_version_key) — по первому числу
    # после «v», а НЕ строгим `schema_vN.sql$`. Иначе файл с суффиксом
    # (schema_v146_ban_weather.sql) считался здесь версией 0, и линтер проверял
    # НЕ ТОТ порядок, в котором миграции реально применяются.
    m = re.search(r"schema_v(\d+)", name)
    return int(m.group(1)) if m else 0


def _schema_files() -> list[str]:
    # тот же набор, что применяет create_pool: schema*.sql в корне и database/
    paths = glob.glob(os.path.join(_ROOT, "schema*.sql")) + glob.glob(
        os.path.join(_ROOT, "database", "schema*.sql")
    )
    # дедуп по basename, сортировка по версии
    seen: dict[str, str] = {}
    for p in paths:
        seen.setdefault(os.path.basename(p), p)
    return sorted(seen.values(), key=_version)


def test_no_reference_to_nonexistent_users_table():
    offenders = []
    for path in _schema_files():
        with open(path, encoding="utf-8") as f:
            if re.search(r"REFERENCES\s+users\s*\(", f.read(), re.IGNORECASE):
                offenders.append(os.path.basename(path))
    assert not offenders, (
        f"REFERENCES users(...) — таблицы users нет, используйте platform_users(user_id): {offenders}"
    )


def test_no_volatile_function_in_index_predicate():
    # CREATE INDEX ... WHERE ... NOW()/CURRENT_TIMESTAMP/CURRENT_DATE — не IMMUTABLE
    idx_re = re.compile(
        r"CREATE\s+INDEX[^;]*?\bWHERE\b[^;]*?(NOW\s*\(|CURRENT_TIMESTAMP|CURRENT_DATE|CURRENT_TIME)\b",
        re.IGNORECASE | re.DOTALL,
    )
    offenders = []
    for path in _schema_files():
        with open(path, encoding="utf-8") as f:
            if idx_re.search(f.read()):
                offenders.append(os.path.basename(path))
    assert not offenders, (
        f"NOW()/CURRENT_* в предикате индекса (функции должны быть IMMUTABLE): {offenders}"
    )


def test_index_target_table_created_no_later_than_index():
    """Таблица под CREATE INDEX должна создаваться в файле с номером <= файла индекса."""
    # где создаётся каждая таблица (минимальная версия)
    created_at: dict[str, int] = {}
    table_re = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)", re.IGNORECASE
    )
    for path in _schema_files():
        v = _version(path)
        with open(path, encoding="utf-8") as f:
            for t in table_re.findall(f.read()):
                created_at.setdefault(t.lower(), v)

    index_re = re.compile(
        r"CREATE\s+INDEX[^;]*?\bON\s+([a-z_][a-z0-9_]*)\s*\(", re.IGNORECASE | re.DOTALL
    )
    offenders = []
    for path in _schema_files():
        v = _version(path)
        with open(path, encoding="utf-8") as f:
            for tbl in index_re.findall(f.read()):
                tv = created_at.get(tbl.lower())
                # проверяем только таблицы, которые вообще создаются в schema_v* файлах
                if tv is not None and tv > v:
                    offenders.append(f"{os.path.basename(path)}: индекс на {tbl} (создан в v{tv})")
    assert not offenders, "CREATE INDEX раньше CREATE TABLE:\n" + "\n".join(offenders)

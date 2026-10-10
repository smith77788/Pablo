"""Один и тот же индекс не заводится дважды под разными именами.

История схемы — 220+ файлов, и автор очередного не находил уже существующий
индекс, а создавал свой. Накопилось десять групп точных дублей: та же таблица,
те же колонки в том же порядке, то же условие. Дубль замедляет КАЖДУЮ вставку
и обновление строки (пишется он тоже), занимает место, греет кеш страницами,
которые никто не читает, и удлиняет VACUUM. Дороже всего это на
`operation_queue` и `operation_log` — туда пишет каждая массовая операция.

Дубли сняты в `schema_v224_drop_duplicate_indexes.sql`. Этот тест сторожит,
чтобы они не вернулись следующим файлом схемы.

Чего тест НЕ видит: индексы, которые Postgres создаёт сам под ограничением
(`UNIQUE`/`PRIMARY KEY` прямо в `CREATE TABLE`). Дубль такого индекса ловится
только на живой базе — так нашёлся `managed_bots_bot_id_unique`.
"""
from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CREATE = re.compile(
    r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([A-Za-z0-9_]+)\s+ON\s+([A-Za-z0-9_.]+)\s*(?:USING\s+\w+\s*)?\(([^;]*?)\)"
    r"\s*(?:WHERE\s+([^;]+?))?\s*;",
    re.I | re.S,
)
_DROP = re.compile(
    r"DROP\s+INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([A-Za-z0-9_]+)", re.I)


def _norm(text: str | None) -> str:
    """Свести написание к сравнимому виду: пробелы, регистр, скобки."""
    out = re.sub(r"\s+", " ", (text or "")).strip().lower()
    return out.replace("( ", "(").replace(" )", ")")


def _version_key(path: str) -> tuple[int, str]:
    m = re.search(r"schema_v(\d+)", os.path.basename(path))
    return (int(m.group(1)) if m else 0, os.path.basename(path))


def _schema_files() -> list[str]:
    paths = set(glob.glob(os.path.join(ROOT, "schema*.sql")))
    paths |= set(glob.glob(os.path.join(ROOT, "database", "schema*.sql")))
    return sorted(paths, key=_version_key)


def _index_signatures() -> dict[tuple, list[tuple[str, str]]]:
    """Подпись индекса → кто её занимает. Удалённые миграцией не считаются."""
    created: dict[str, tuple] = {}
    dropped: set[str] = set()
    for path in _schema_files():
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for m in _CREATE.finditer(src):
            uniq, name, table, cols, pred = m.groups()
            created[name.lower()] = (
                bool(uniq), _norm(table), _norm(cols), _norm(pred),
                os.path.basename(path))
        for m in _DROP.finditer(src):
            dropped.add(m.group(1).lower())

    by_sig: dict[tuple, list[tuple[str, str]]] = {}
    for name, (uniq, table, cols, pred, filename) in created.items():
        if name in dropped:
            continue
        by_sig.setdefault((uniq, table, cols, pred), []).append((name, filename))
    return by_sig


def test_schema_parses_into_something_sensible():
    """Сначала проверяем измеритель: разбор, который ничего не нашёл, «докажет»
    отсутствие дублей на любой схеме."""
    sigs = _index_signatures()
    assert len(sigs) > 300, (
        f"разбор нашёл всего {len(sigs)} индексов — сломался сам разбор, "
        "а не схема")


def test_no_two_indexes_with_the_same_signature():
    dupes = {sig: names for sig, names in _index_signatures().items()
             if len(names) > 1}
    if not dupes:
        return
    lines = []
    for (uniq, table, cols, pred), names in sorted(dupes.items(),
                                                   key=lambda kv: kv[0][1]):
        where = f" WHERE {pred}" if pred else ""
        lines.append(f"  {table} ({cols}){where}{' UNIQUE' if uniq else ''}")
        for name, filename in names:
            lines.append(f"      {name}  <- {filename}")
    raise AssertionError(
        "Один и тот же индекс заведён под разными именами. Лишний индекс "
        "замедляет каждую вставку в таблицу и ничего не ускоряет — оставьте "
        "один, а остальные снимите новым файлом схемы (DROP INDEX IF EXISTS):\n"
        + "\n".join(lines))


def test_drop_migration_is_the_last_word():
    """Файл с удалением должен применяться ПОСЛЕ тех, что создают дубли, —
    иначе на чистой базе индексы вернутся."""
    files = _schema_files()
    drops = [f for f in files
             if _DROP.search(open(f, encoding="utf-8", errors="replace").read())]
    assert drops, "файл с удалением дублей исчез"
    last_drop = max(_version_key(f) for f in drops)
    creators = {}
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for m in _CREATE.finditer(src):
            creators[m.group(2).lower()] = path
    dropped_names = set()
    for path in drops:
        with open(path, encoding="utf-8", errors="replace") as fh:
            dropped_names |= {m.group(1).lower() for m in _DROP.finditer(fh.read())}
    for name in dropped_names:
        src_file = creators.get(name)
        if src_file is None:
            continue
        assert _version_key(src_file) < last_drop, (
            f"{name} создаётся в {os.path.basename(src_file)} ПОСЛЕ удаления — "
            "на чистой базе дубль вернётся")

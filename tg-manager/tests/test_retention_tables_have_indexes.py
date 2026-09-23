"""У каждой таблицы, которую чистит уборщик, должен быть индекс по её дате.

Уборка (`services/db_maintenance`) ходит по списку `_RETENTION` и удаляет из
каждой таблицы строки старше срока. Удаление идёт пачками по ctid, и каждая
пачка заново ищет подходящие строки. Без индекса по колонке даты этот поиск —
полный проход по таблице: на журнале в миллионы строк одна пачка занимает
минуты, проход упирается в потолок и не доезжает, таблица продолжает расти, а
следующая уборка стоит ещё дороже. Так уже было: `_prune_batched` появился
именно после того, как уборка не могла закончиться ни разу.

Частичный индекс (`... WHERE status = ...`) здесь не считается: уборка идёт по
всей таблице, а такой индекс покрывает только её кусок.

Отдельно проверяется, что колонка даты вообще существует. Опечатка в имени
колонки не видна ниоткуда: уборка ловит исключение, пишет строку в лог и
молча не чистит эту таблицу годами.

Детектор проверяется на заведомо здоровом и заведомо больном примере
(`test_детектор_*`) — иначе непонятно, что он вообще что-то ловит.
"""
from __future__ import annotations

import glob
import os
import re

from services.db_maintenance import _RETENTION

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?"
    r"\s+\S+\s+ON\s+([A-Za-z0-9_.]+)\s*\(([^)]*)\)([^;]*)",
    re.I,
)
_TABLE_RE = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+([A-Za-z0-9_.]+)\s*\((.*?)\n\);", re.S | re.I
)
# Имена таблиц бывают с цифрами (b2b_leads) — без \\d детектор молча их пропускал.
_ALTER_RE = re.compile(
    r"ALTER TABLE(?:\s+IF EXISTS)?\s+([A-Za-z0-9_.]+)\s+ADD COLUMN(?:\s+IF NOT EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.I,
)


def _bare(name: str) -> str:
    return name.split(".")[-1].strip('"').lower()


def разобрать_схему(sql: str, колонки: dict, ведущие: dict) -> None:
    """Собрать колонки таблиц и ведущие колонки ПОЛНЫХ индексов."""
    for m in _TABLE_RE.finditer(sql):
        table, body = _bare(m.group(1)), m.group(2)
        for line in body.splitlines():
            line = line.strip().rstrip(",")
            col = re.match(r"([a-z_][a-z0-9_]*)\s+[A-Za-z]", line)
            if col and col.group(1).lower() not in ("primary", "unique", "constraint", "check", "foreign"):
                колонки.setdefault(table, set()).add(col.group(1).lower())
    for m in _ALTER_RE.finditer(sql):
        колонки.setdefault(_bare(m.group(1)), set()).add(m.group(2).lower())
    for m in _INDEX_RE.finditer(sql):
        table, cols, tail = _bare(m.group(1)), m.group(2), m.group(3)
        if re.search(r"\bWHERE\b", tail, re.I):
            continue  # частичный индекс не покрывает уборку по всей таблице
        first = cols.split(",")[0].strip().split()[0].strip('"').lower()
        ведущие.setdefault(table, set()).add(first)


def _схема_репозитория() -> tuple[dict, dict]:
    колонки: dict[str, set[str]] = {}
    ведущие: dict[str, set[str]] = {}
    for path in sorted(glob.glob(os.path.join(_ROOT, "schema*.sql"))):
        with open(path, encoding="utf-8") as f:
            разобрать_схему(f.read(), колонки, ведущие)
    return колонки, ведущие


def test_колонка_уборки_существует():
    колонки, _ = _схема_репозитория()
    пропажи = [
        f"{table}.{column}"
        for table, column, _ in _RETENTION
        if column.lower() not in колонки.get(table.lower(), set())
    ]
    assert not пропажи, (
        "уборщик чистит по колонкам, которых нет в схеме: "
        + ", ".join(пропажи)
        + ". Запрос будет падать каждые шесть часов, а таблица — расти."
    )


def test_у_каждой_чистимой_таблицы_есть_индекс_по_дате():
    _, ведущие = _схема_репозитория()
    без_индекса = [
        f"{table}.{column}"
        for table, column, _ in _RETENTION
        if column.lower() not in ведущие.get(table.lower(), set())
    ]
    assert not без_индекса, (
        "нет индекса по колонке уборки: "
        + ", ".join(без_индекса)
        + ". Каждая пачка удаления будет читать таблицу целиком — уборка не "
        "закончится. Заведите новый schema_vN.sql с "
        "CREATE INDEX IF NOT EXISTS ... (без WHERE)."
    )


# ── Проверка самого детектора ──────────────────────────────────────────────


_ЗДОРОВЫЙ = """
CREATE TABLE IF NOT EXISTS t_ok (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_t_ok_occurred ON t_ok(occurred_at DESC);
"""

_БОЛЬНОЙ = """
CREATE TABLE IF NOT EXISTS t_bad (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT
);
CREATE INDEX IF NOT EXISTS idx_t_bad_partial ON t_bad(occurred_at) WHERE status = 'error';
CREATE INDEX IF NOT EXISTS idx_t_bad_other ON t_bad(status, occurred_at);
"""


def test_детектор_видит_здоровую_таблицу():
    колонки, ведущие = {}, {}
    разобрать_схему(_ЗДОРОВЫЙ, колонки, ведущие)
    assert "occurred_at" in колонки["t_ok"]
    assert "occurred_at" in ведущие["t_ok"]


def test_детектор_не_засчитывает_частичный_и_неведущий_индекс():
    колонки, ведущие = {}, {}
    разобрать_схему(_БОЛЬНОЙ, колонки, ведущие)
    assert "occurred_at" in колонки["t_bad"]
    assert "occurred_at" not in ведущие["t_bad"], "частичный индекс засчитан как полный"
    assert ведущие["t_bad"] == {"status"}


def test_детектор_видит_колонку_добавленную_альтером():
    колонки, ведущие = {}, {}
    разобрать_схему(
        "CREATE TABLE IF NOT EXISTS t2 (\n    id BIGSERIAL PRIMARY KEY\n);\n"
        "ALTER TABLE t2 ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ;",
        колонки, ведущие,
    )
    assert "checked_at" in колонки["t2"]

"""Каждая таблица, к которой ходит код, обязана создаваться миграцией.

ЗАЧЕМ. Так был найден мёртвый раздел «Воркфлоу»: `workflow_definitions` и
`workflow_runs` заводила только функция `init_workflow_tables`, которую НИКТО не
вызывает, и ни одна миграция их не создавала. Экран выглядел рабочим и не
работал ни в одной своей части. Тем же способом нашлись ещё два места:
  • `ranking_alerts` — оповещения о позициях не записывались, а падение на их
    подсчёте роняло весь блок статистики раздела;
  • `strike_appeals` — письма-апелляции реально уходили, а запись о поданной
    апелляции терялась молча, и статус узнать было нельзя;
  • `UPDATE operations` в автовосстановлении — таблица называется
    operation_queue, и восстановление зависшей операции не срабатывало ни разу.

Ошибка этого класса не видна ни на старте, ни в юнит-тестах с заглушкой пула:
запрос падает у пользователя, а `except Exception` превращает падение в пустой
экран. Поэтому проверка статическая и без БД — она обязана идти в каждом
прогоне, а не только там, где поднят Postgres.

ИСТОЧНИК ПРАВДЫ — файлы миграций schema*.sql. Именно они накатываются на
проде. Таблицы, создаваемые кодом в рантайме, перечислены ниже поимённо: список
намеренно короткий, потому что «создам сам при первом обращении» — это ровно тот
приём, из-за которого раздел «Воркфлоу» и умер.
"""
from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "tests"}

# Ссылка на таблицу в SQL.
_REF = re.compile(
    r"\b(?:FROM|JOIN|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?"
    r"([a-z_][a-z0-9_]{3,})\b", re.I)
# Имена CTE (`WITH x AS (`, `, y AS (`) — это не таблицы.
_CTE = re.compile(
    r"(?:\bWITH\b(?:\s+RECURSIVE)?|,)\s*([a-z_][a-z0-9_]*)\s+AS\s*"
    r"(?:MATERIALIZED\s*)?\(", re.I)
# `EXTRACT(EPOCH FROM col)` — FROM здесь не про таблицу.
_EXTRACT = re.compile(r"\bEXTRACT\s*\([^)]*\)", re.I)
# `FOR UPDATE SKIP LOCKED` — UPDATE здесь не про таблицу.
_FOR_UPDATE = re.compile(r"\bFOR\s+(?:NO\s+KEY\s+)?(?:UPDATE|SHARE)\b", re.I)

_CREATE_TABLE = re.compile(
    r"CREATE\s+(?:UNLOGGED\s+|TEMP(?:ORARY)?\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?", re.I)
_CREATE_VIEW = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?", re.I)

# Не таблицы: конструкции SQL и системные каталоги.
_NOT_TABLES = {"lateral", "dual", "generate_series", "unnest",
               "jsonb_array_elements", "json_array_elements", "jsonb_each",
               "jsonb_to_recordset", "values", "select", "only"}
_SYSTEM_PREFIXES = ("pg_", "information_schema", "sqlite_")

# Таблицы, которые заводит не миграция. Каждая — с причиной; список не должен
# расти: «создам сам при первом обращении» переживает ровно до момента, когда
# создателя перестают вызывать.
RUNTIME_CREATED: dict[str, str] = {
    "fsm_state": "pg_fsm_storage создаёт при инициализации хранилища FSM бота",
    "schema_migrations": "журнал самих миграций, создаётся до их накатывания",
    "automation_workflows": "создаётся кодом database.db при старте",
    "workflow_step_runs": "создаётся кодом database.db при старте",
    "workflow_step_logs": "создаётся кодом database.db при старте",
    "sessions": "таблица ВНУТРИ .session-файла Telethon (SQLite), не наша схема",
}


def _declared_by_migrations() -> set[str]:
    names: set[str] = set()
    for fname in os.listdir(ROOT):
        if not (fname.startswith("schema") and fname.endswith(".sql")):
            continue
        src = open(os.path.join(ROOT, fname), encoding="utf-8", errors="ignore").read()
        names |= {m.group(1).lower() for m in _CREATE_TABLE.finditer(src)}
        names |= {m.group(1).lower() for m in _CREATE_VIEW.finditer(src)}
    return names


def _sql_literals():
    """(файл, строка, текст) для строк, похожих на SQL. Докстринги пропускаем."""
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            try:
                tree = ast.parse(open(path, encoding="utf-8", errors="ignore").read())
            except SyntaxError:
                continue
            docs = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef,
                                      ast.AsyncFunctionDef, ast.ClassDef))}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)):
                    continue
                sql = node.value
                if sql in docs or len(sql) < 20:
                    continue
                if not re.search(r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b", sql):
                    continue
                yield rel, node.lineno, sql


def unknown_tables() -> dict[str, list[str]]:
    known = _declared_by_migrations() | set(RUNTIME_CREATED)
    out: dict[str, list[str]] = {}
    for rel, lineno, sql in _sql_literals():
        ctes = {m.group(1).lower() for m in _CTE.finditer(sql)}
        cleaned = _FOR_UPDATE.sub(" ", _EXTRACT.sub(" ", sql))
        for m in _REF.finditer(cleaned):
            name = m.group(1).lower()
            if (name in known or name in ctes or name in _NOT_TABLES
                    or name.startswith(_SYSTEM_PREFIXES)):
                continue
            out.setdefault(name, []).append(f"{rel}:{lineno}")
    return out


# ── Тесты ─────────────────────────────────────────────────────────────────────

def test_no_sql_against_tables_that_no_migration_creates():
    bad = unknown_tables()
    assert not bad, (
        "код ходит в таблицы, которых не создаёт ни одна миграция:\n"
        + "\n".join(f"  {name}: {', '.join(where[:3])}"
                    for name, where in sorted(bad.items()))
        + "\n\nЛибо опечатка в имени, либо таблицу забыли завести миграцией. "
          "И то и другое выглядит снаружи как пустой экран: запрос падает, "
          "а except Exception превращает падение в «данных нет»."
    )


def test_migration_files_are_actually_parsed():
    """Пустой список известных таблиц сделал бы тест зелёным навсегда."""
    declared = _declared_by_migrations()
    assert len(declared) > 150, f"из миграций разобрано всего {len(declared)} таблиц"
    for must in ("tg_accounts", "operation_queue", "workflow_definitions",
                 "ranking_alerts", "strike_appeals", "op_circuit_breaker"):
        assert must in declared, f"{must} не найдена в миграциях"


def test_sql_literals_are_actually_scanned():
    n = sum(1 for _ in _sql_literals())
    assert n > 500, f"найдено всего {n} SQL-строк — сканер, похоже, сломан"


def test_detector_catches_a_typo():
    """Детектор обязан ловить именно тот случай, ради которого написан."""
    known = _declared_by_migrations() | set(RUNTIME_CREATED)
    assert "operations" not in known, (
        "имя `operations` не должно быть известным: именно опечатка в нём "
        "ломала автовосстановление операций (таблица — operation_queue)")
    sql = "UPDATE operations SET status = 'pending' WHERE id = $1"
    hits = [m.group(1).lower() for m in _REF.finditer(sql)]
    assert hits == ["operations"]


def test_detector_ignores_cte_and_extract():
    """Ложные срабатывания обесценивают проверку: её начнут обходить."""
    sql = ("WITH daily AS (SELECT 1), hourly AS (SELECT 2) "
           "SELECT EXTRACT(EPOCH FROM last_success_at) FROM daily "
           "JOIN hourly ON TRUE FOR UPDATE SKIP LOCKED")
    ctes = {m.group(1).lower() for m in _CTE.finditer(sql)}
    cleaned = _FOR_UPDATE.sub(" ", _EXTRACT.sub(" ", sql))
    left = [m.group(1).lower() for m in _REF.finditer(cleaned)
            if m.group(1).lower() not in ctes]
    assert left == [], f"ложные срабатывания: {left}"


def test_runtime_created_list_stays_short():
    """«Создам сам при первом обращении» — тот самый приём, что убил «Воркфлоу»."""
    assert len(RUNTIME_CREATED) <= 6, (
        f"таблиц, создаваемых в обход миграций, стало {len(RUNTIME_CREATED)}. "
        "Заводите таблицы миграцией: создатель, которого перестали вызывать, "
        "не оставляет следа — раздел просто перестаёт работать."
    )
    assert all(len(v.strip()) > 10 for v in RUNTIME_CREATED.values()), \
        "каждая запись обязана объяснять, кто и когда создаёт таблицу"

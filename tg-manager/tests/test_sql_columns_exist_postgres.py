"""Каждая колонка в INSERT/UPDATE обязана существовать в боевой схеме.

ЗАЧЕМ. Проверка таблиц (tests/test_sql_tables_exist_in_schema.py) ловит случай,
когда таблицы нет вовсе. Но так же тихо ломается запрос к СУЩЕСТВУЮЩЕЙ таблице с
чужими колонками, и наружу это выходит ровно так же — пустым экраном. Найдено
этой проверкой:
  • `services/ranking_engine` целиком работал по модели «владелец + канал»
    (`tracked_keywords.channel_id`, `search_rankings.previous_position`), которой
    в схеме нет: экран «Рейтинг» показывал «Нет ключевых слов» всегда;
  • `UPDATE tg_accounts SET needs_reauth = TRUE` — такой колонки нет, поэтому
    аккаунт с мёртвой сессией НЕ помечался и продолжал разбирать задачи;
  • `UPDATE user_proxies SET fail_count = fail_count + 1` — колонки нет, счётчик
    сбоев прокси не рос ни разу (настоящая — consecutive_failures);
  • `cf_worker_pool.fail_streak` — колонки не было, и дебаунс «воркер упал» не
    работал: разовый блип сразу гнал аккаунты на другой воркер, то есть менял им
    exit-IP, а это AUTH_KEY_DUPLICATED.

Схему берём НАСТОЯЩУЮ — из базы после накатывания всех миграций. Разбирать
schema*.sql текстом нельзя: часть колонок добавляется не в CREATE TABLE, и
парсер давал бы ложные срабатывания, а детектор с ложными срабатываниями
обесценивает сам себя.

Запуск: INFRAGRAM_TEST_DSN=... pytest tests/test_sql_columns_exist_postgres.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "tests"}

_INSERT = re.compile(
    r"INSERT\s+INTO\s+(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?\s*\(([^)]*)\)", re.I)
_UPDATE = re.compile(
    r"\bUPDATE\s+(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?\s+SET\s+(.*?)"
    r"(?:\bWHERE\b|\bRETURNING\b|\bFROM\b|$)", re.I | re.S)
_ASSIGN = re.compile(r"(?:^|,)\s*\"?([a-z_][a-z0-9_]*)\"?\s*=", re.I)
# Только ПРОСТЫЕ однотабличные SELECT: где есть JOIN, псевдоним или выражение,
# принадлежность колонки неоднозначна, и детектор начал бы врать.
_SELECT = re.compile(
    r"SELECT\s+(?!.*\bJOIN\b)(.*?)\s+FROM\s+(?:public\.)?\"?([a-z_][a-z0-9_]*)\"?"
    r"\s*(?:WHERE|ORDER|GROUP|LIMIT|$)", re.I | re.S)
# Системные колонки Postgres есть у любой таблицы, но не в information_schema.
_SYSTEM_COLUMNS = {"ctid", "xmin", "xmax", "cmin", "cmax", "tableoid", "oid"}

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def schema():
    """{'таблица.колонка'} после ПОЛНОГО накатывания миграций на чистую базу."""
    import asyncpg
    from database import db as _db

    async def _prepare():
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute("DROP DATABASE IF EXISTS colcheck")
            await admin.execute("CREATE DATABASE colcheck")
        finally:
            await admin.close()

    _run(_prepare())
    os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test")
    os.environ.setdefault("TG_API_ID", "1")
    os.environ.setdefault("TG_API_HASH", "x")
    os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "t")

    # DATABASE_URL читается на импорте — подменяем значение в самом модуле.
    saved = _db.DATABASE_URL
    _db.DATABASE_URL = re.sub(r"/([^/?]+)\?", "/colcheck?", DSN, count=1)
    try:
        pool = _run(_db.create_pool())
    finally:
        _db.DATABASE_URL = saved

    # Вторая, встроенная цепочка миграций: список DDL в mini_app_api,
    # выполняемый на app.on_startup. Схема на проде — результат ОБЕИХ, поэтому
    # снимок обязан включать и её, иначе тест ловил бы несуществующие ошибки.
    # (Две параллельные цепочки — отдельный долг, но проверка колонок должна
    # отражать боевую реальность, а не спорить с ней.)
    from services.mini_app_api import INLINE_MIGRATIONS

    async def _inline():
        for stmt in INLINE_MIGRATIONS:
            try:
                await pool.execute(stmt)
            except Exception:  # как в проде: сбой отдельного DDL не валит старт
                pass

    _run(_inline())

    rows = _run(pool.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public'"))
    _run(pool.close())
    return {f"{r['table_name']}.{r['column_name']}" for r in rows}


def _sql_literals():
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
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value not in docs and len(node.value) > 20):
                    yield rel, node.lineno, node.value


def _strip_parens(text: str) -> str:
    """Убрать содержимое скобок: внутри — аргументы функций, а не колонки."""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"\([^()]*\)", " ", text)
    return text


def unknown_columns(schema: set[str]) -> dict[str, list[str]]:
    tables = {c.split(".", 1)[0] for c in schema}
    bad: dict[str, list[str]] = {}

    def _note(table: str, col: str, where: str):
        if col in _SYSTEM_COLUMNS:
            return
        key = f"{table}.{col}"
        if key not in schema:
            bad.setdefault(key, []).append(where)

    for rel, lineno, sql in _sql_literals():
        where = f"{rel}:{lineno}"
        for m in _INSERT.finditer(sql):
            table = m.group(1).lower()
            if table not in tables:
                continue          # отсутствие таблицы ловит соседняя проверка
            for raw in m.group(2).split(","):
                col = raw.strip().strip('"').lower()
                if re.fullmatch(r"[a-z_][a-z0-9_]*", col or ""):
                    _note(table, col, where)
        for m in _UPDATE.finditer(sql):
            table = m.group(1).lower()
            if table not in tables:
                continue
            for a in _ASSIGN.finditer(_strip_parens(m.group(2))):
                _note(table, a.group(1).lower(), where)
        for m in _SELECT.finditer(sql):
            select_list, table = m.group(1), m.group(2).lower()
            if table not in tables:
                continue
            low = select_list.lower()
            if "*" in select_list or "(" in select_list or " as " in low:
                continue          # выражения и псевдонимы не разбираем
            for raw in select_list.split(","):
                col = raw.strip().strip('"').lower()
                if re.fullmatch(r"[a-z_][a-z0-9_]*", col or ""):
                    _note(table, col, where)
    return bad


# ── Тесты ─────────────────────────────────────────────────────────────────────

def test_no_writes_to_columns_that_do_not_exist(schema):
    bad = unknown_columns(schema)
    assert not bad, (
        "запись в колонки, которых нет в схеме:\n"
        + "\n".join(f"  {name}: {', '.join(sorted(set(w))[:3])}"
                    for name, w in sorted(bad.items()))
        + "\n\nТакой запрос падает всегда, а except превращает падение в пустой "
          "экран: снаружи это «функция просто не работает»."
    )


def test_schema_snapshot_is_not_empty(schema):
    """Пустая схема сделала бы проверку зелёной навсегда."""
    assert len(schema) > 1500, f"из базы прочитано всего {len(schema)} колонок"
    for must in ("tg_accounts.id", "operation_queue.status",
                 "tracked_keywords.region", "cf_worker_pool.fail_streak",
                 "ranking_alerts.keyword_id"):
        assert must in schema, f"{must} нет в схеме после миграций"


def test_detector_catches_a_planted_column(schema):
    """Детектор обязан ловить именно то, ради чего написан."""
    assert "tg_accounts.needs_reauth" not in schema, (
        "колонки needs_reauth нет — именно на ней падало автовосстановление "
        "сессии, и аккаунт с мёртвой сессией продолжал разбирать задачи")
    sql = "UPDATE tg_accounts SET needs_reauth = TRUE WHERE id = $1"
    cols = [m.group(1) for m in _ASSIGN.finditer(_strip_parens(
        _UPDATE.search(sql).group(2)))]
    assert cols == ["needs_reauth"]


def test_detector_ignores_function_arguments():
    """`SET x = GREATEST(a, b)` не должен выглядеть как запись в колонку b."""
    sql = ("UPDATE t SET a = GREATEST(t.a, EXCLUDED.a), "
           "b = COALESCE(b, 0) + 1 WHERE id = $1")
    cols = [m.group(1) for m in _ASSIGN.finditer(_strip_parens(
        _UPDATE.search(sql).group(2)))]
    assert cols == ["a", "b"], f"ложные срабатывания: {cols}"

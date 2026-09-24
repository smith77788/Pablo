"""Каждый запрос продукта обязан быть исполнимым на настоящей схеме.

ЗАЧЕМ. Соседние проверки разбирают SQL регулярками и поэтому видят лишь часть:
`test_sql_tables_exist_in_schema` ловит отсутствующую таблицу,
`test_sql_columns_exist_postgres` — колонку в INSERT/UPDATE и в простом SELECT.
Мимо них проходит всё остальное: колонка в WHERE и в JOIN, группировка, которую
Postgres не принимает, сравнение jsonb со строкой, функция с неверным именем
аргумента. Такой запрос падает КАЖДЫЙ раз, его гасит `except`, и наружу это
выходит нулём или пустым экраном — «функция просто не работает».

ЧТО ДЕЛАЕТ. Отдаёт каждый запрос самому Postgres командой PREPARE: тот
разбирает и планирует его по настоящей схеме, но не исполняет. Никаких
регулярок — судит база. Так найдено:
  • весь `services/audience_analytics` работал по колонке `channel_id`, которой
    нет ни в bot_users, ни в user_activity, ни в content_performance (там
    bot_id): 17 запросов, каждый падал всегда;
  • живой поток операций в мини-аппе звал `make_interval(minutes => 30)` —
    аргумент называется `mins`, и владелец не видел ни одного «операция
    завершилась»;
  • ветка авто-восстановления по доле сбоев аккаунта читала
    `operation_queue.account_id`, которого нет, и не срабатывала ни разу;
  • интеллект-слой читал `tg_accounts.health_score` (пульс живёт в
    account_health_history) и не получал НИ ОДНОГО аккаунта;
  • `(a.flood_count_7d or 0) > 5` — питоновский `or` внутри SQL;
  • счётчик контактов с телефоном сравнивал jsonb с пустой строкой;
  • карта инфраструктуры группировала по `b.bot_id`, который не первичный ключ.

ЧТО ПРОВЕРЯЕТСЯ. Только готовый запрос: строковая константа прямо в вызове
(`pool.fetch("…")`) или константа модуля, переданная по имени
(`pool.fetch(_BURNED_SQL, …)`). Запросы, собираемые конкатенацией, сюда не
берутся сознательно: у них на руках половина текста, и судить о ней нельзя —
детектор с ложными срабатываниями обесценивает сам себя.

КАК ЗАПУСТИТЬ (2 минуты, Postgres 16) — см. докстринг
tests/test_invite_e2e_postgres.py; переменная та же, INFRAGRAM_TEST_DSN.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. докстринг)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "tests"}

# Имена методов драйвера, первый аргумент которых — готовый запрос.
_DRIVER_CALLS = {"fetch", "fetchrow", "fetchval", "execute", "executemany"}
_STARTS = re.compile(r"^\s*(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WITH)\b", re.I)
_DDL = re.compile(r"^\s*CREATE\s+(TABLE|INDEX|UNIQUE)", re.I)

# Осознанные исключения. Каждое — с причиной; список короткий намеренно.
_ALLOWED = (
    # Запросы к SQLite: файл сессии Telethon и загруженный владельцем список.
    ("services/account_manager.py", "FROM sessions"),
    ("services/session_converter.py", "FROM sessions"),
    ("services/invite_list_parser.py", "sqlite_master"),
    # Расширение pg_stat_statements включено не везде; запрос сам это переживает.
    ("database/db.py", "pg_stat_statements"),
)

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _py_files():
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
            yield rel, tree


def _module_sql_constants(tree) -> dict[str, str]:
    """Строковые константы модуля: NAME = "SELECT …".

    Запрос, вынесенный в такую константу, — тот же готовый запрос, просто
    названный. Без этого шага мимо проверки проходил, например,
    `budget_radar._BURNED_SQL`, который читал tg_accounts.created_at (в схеме
    added_at) и вместо числа сожжённых аккаунтов отдавал ошибку.
    """
    out: dict[str, str] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def driver_queries():
    """(файл, строка, SQL) для запросов, целиком переданных драйверу."""
    for rel, tree in _py_files():
        constants = _module_sql_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            if name not in _DRIVER_CALLS:
                continue
            arg = node.args[0]
            sql = None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                sql = arg.value
            elif isinstance(arg, ast.Name):
                sql = constants.get(arg.id)
            if sql and _STARTS.match(sql):
                yield rel, arg.lineno, sql


def _runtime_ddl():
    """CREATE TABLE/INDEX IF NOT EXISTS из кода: часть таблиц заводит модуль.

    Например fsm_state создаёт services/pg_fsm_storage на старте. Без этого шага
    снимок схемы не совпал бы с боевым, и проверка выдумывала бы находки.
    """
    out = []
    for _rel, tree in _py_files():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and _DDL.match(node.value)):
                out.append(node.value)
    return out


@pytest.fixture(scope="module")
def conn():
    """Соединение с базой, поднятой ОБЕИМИ цепочками миграций плюс DDL из кода."""
    import asyncpg
    from database import db as _db

    async def _fresh():
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute("DROP DATABASE IF EXISTS sqlcheck")
            await admin.execute("CREATE DATABASE sqlcheck")
        finally:
            await admin.close()

    try:
        _run(_fresh())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")

    os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test")
    os.environ.setdefault("TG_API_ID", "1")
    os.environ.setdefault("TG_API_HASH", "x")
    os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "t")

    saved = _db.DATABASE_URL
    _db.DATABASE_URL = re.sub(r"/[^/?]+(\?|$)", r"/sqlcheck\1", DSN, count=1)
    try:
        pool = _run(_db.create_pool())
    finally:
        _db.DATABASE_URL = saved

    # Вторая цепочка: DDL, выполняемый на app.on_startup мини-аппом.
    from services.mini_app_api import INLINE_MIGRATIONS

    async def _rest():
        for stmt in list(INLINE_MIGRATIONS) + _runtime_ddl():
            try:
                await pool.execute(stmt)
            except Exception:  # как в проде: сбой отдельного DDL не валит старт
                pass

    _run(_rest())
    yield pool
    _run(pool.close())


def _allowed(rel: str, sql: str) -> bool:
    return any(rel == f and marker in sql for f, marker in _ALLOWED)


def broken_queries(conn) -> list[tuple[str, str, str]]:
    """[(где, ошибка, начало запроса)] — запросы, которые Postgres не принял."""
    async def _check():
        bad: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for i, (rel, lineno, sql) in enumerate(driver_queries()):
            if sql in seen or _allowed(rel, sql):
                continue
            seen.add(sql)
            try:
                await conn.execute(f"PREPARE _sqlcheck_{i} AS {sql}")
                await conn.execute(f"DEALLOCATE _sqlcheck_{i}")
            except Exception as exc:
                # Тип параметра, который Postgres не может вывести из текста,
                # — не про схему: драйвер получает его от asyncpg.
                if type(exc).__name__ == "IndeterminateDatatypeError":
                    continue
                bad.append((f"{rel}:{lineno}",
                            str(exc).split("\n")[0][:120],
                            " ".join(sql.split())[:110]))
        return bad

    return _run(_check())


# ── Тесты ─────────────────────────────────────────────────────────────────────

def test_every_query_is_accepted_by_postgres(conn):
    bad = broken_queries(conn)
    assert not bad, (
        "запросы, которые Postgres не принимает (значит, падают всегда):\n"
        + "\n".join(f"  {w}\n    {err}\n    {sql}" for w, err, sql in bad)
        + "\n\nТакой запрос гасит except, и владелец видит ноль или пустой "
          "экран вместо ошибки."
    )


def test_probe_actually_reads_the_product(conn):
    """Пробник, которому нечего проверять, был бы зелёным на любом коде."""
    total = len({sql for _rel, _ln, sql in driver_queries()})
    assert total > 2000, f"собрано всего {total} запросов — пробник сломан"


def test_schema_snapshot_is_complete(conn):
    cols = _run(conn.fetchval(
        "SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='public'"))
    assert cols > 2500, f"в снимке схемы всего {cols} колонок"
    for table, column in (("tg_accounts", "added_at"),
                          ("bot_users", "bot_id"),
                          ("account_health_history", "recorded_at"),
                          ("fsm_state", "destiny")):
        got = _run(conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 AND column_name=$2",
            table, column))
        assert got == 1, f"{table}.{column} нет в снимке — схема накатана не вся"


def test_probe_catches_a_broken_query(conn):
    """Проверка измерителя на заведомо сломанном и заведомо здоровом запросе."""
    async def _try(sql):
        try:
            await conn.execute(f"PREPARE _probe_self AS {sql}")
            await conn.execute("DEALLOCATE _probe_self")
            return None
        except Exception as exc:
            return type(exc).__name__

    assert _run(_try("SELECT id FROM tg_accounts WHERE owner_id=$1")) is None, (
        "заведомо здоровый запрос не принят — измеритель врёт")
    assert _run(_try("SELECT id FROM tg_accounts WHERE created_at > now()")) == (
        "UndefinedColumnError"), "измеритель не видит несуществующую колонку"
    assert _run(_try("SELECT b.bot_id, b.username FROM managed_bots b "
                     "GROUP BY b.bot_id")) == "GroupingError", (
        "измеритель не видит неверную группировку")

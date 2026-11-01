"""Снимок схемы обязан давать РОВНО ту же базу, что вся история миграций.

ЗАЧЕМ. Новое окружение (чистая база) поднимается снимком
`database/baseline/baseline.sql`, а не 270+ файлами истории: файлы из
манифеста помечаются применёнными и больше не выполняются никогда. Значит
любое расхождение снимка с историей становится расхождением схемы навсегда,
и проявляется оно не на старте, а на первом запросе к колонке, которой у
нового окружения нет.

ЧТО СРАВНИВАЕТСЯ. Две чистые базы поднимаются НАСТОЯЩИМ стартом
(`database.db.create_pool`): одна снимком, другая — со скрытым снимком, то
есть всей историей. Сравниваются их каталоги: колонки с типами, NULL-ностью и
умолчаниями, индексы, ограничения, функции, триггеры, представления,
последовательности. Порядок колонок не сравнивается: у таблицы, в истории
которой колонку удаляли, номера колонок у снимка честно другие.

Нужен живой Postgres (INFRAGRAM_TEST_DSN); без него файл пропускается.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_BASELINE = "baseline_check_snapshot"
DB_HISTORY = "baseline_check_history"


def _dsn_for(db: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", rf"/{db}\1", DSN, count=1)


_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


_CATALOG_QUERIES = {
    "columns": """
        SELECT table_name || '.' || column_name AS k,
               concat_ws(' | ', data_type, udt_name, is_nullable,
                         character_maximum_length::text, numeric_precision::text,
                         column_default, is_generated, generation_expression) AS v
          FROM information_schema.columns WHERE table_schema = 'public'""",
    "indexes": """
        SELECT indexname AS k, indexdef AS v
          FROM pg_indexes WHERE schemaname = 'public'""",
    "constraints": """
        SELECT c.conrelid::regclass::text || '.' || c.conname AS k,
               pg_get_constraintdef(c.oid) AS v
          FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
         WHERE n.nspname = 'public'""",
    "functions": """
        SELECT p.oid::regprocedure::text AS k, pg_get_functiondef(p.oid) AS v
          FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'public' AND p.prokind IN ('f', 'p')""",
    "triggers": """
        SELECT t.tgrelid::regclass::text || '.' || t.tgname AS k,
               pg_get_triggerdef(t.oid) AS v
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND NOT t.tgisinternal""",
    "views": """
        SELECT viewname AS k, definition AS v
          FROM pg_views WHERE schemaname = 'public'""",
    "sequences": """
        SELECT sequencename AS k,
               concat_ws(' | ', data_type::text, increment_by::text) AS v
          FROM pg_sequences WHERE schemaname = 'public'""",
    "tables": """
        SELECT c.relname AS k, c.relkind::text AS v
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')""",
}


# Выражения (условие частичного индекса, CHECK, тело представления) Postgres
# хранит разобранными и печатает заново. Пройдя через дамп, одно и то же
# выражение печатается иначе: `ARRAY['new'::varchar, 'viewing'::varchar]::text[]`
# у истории и `ARRAY['new'::varchar::text, 'viewing'::varchar::text]` у снимка
# (idx_error_reports_new). Смысл тот же, текст разный. Для этих трёх видов
# сравниваем выражение без приведений типов и скобок; типы колонок при этом
# сравниваются отдельно и без всякой нормализации (вид «columns»).
_DEPARSED = {"indexes", "constraints", "views"}
_CAST = re.compile(r"::[a-z_ ]+(\[\])?(\(\d+(,\d+)?\))?")


def _normalize_expr(text: str) -> str:
    return re.sub(r"[\s()]", "", _CAST.sub("", text or ""))


async def _catalog(pool) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for name, sql in _CATALOG_QUERIES.items():
        rows = await pool.fetch(sql)
        norm = _normalize_expr if name in _DEPARSED else (lambda v: v)
        out[name] = {r["k"]: norm(r["v"]) for r in rows}
    return out


@pytest.fixture(scope="module")
def two_bases():
    """Две чистые базы через настоящий старт: снимком и всей историей."""
    import asyncpg

    os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test")
    os.environ.setdefault("TG_API_ID", "1")
    os.environ.setdefault("TG_API_HASH", "x")
    os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "t")
    from database import db as _db

    sql_path, man_path = _db.baseline_paths(ROOT)
    assert os.path.exists(sql_path) and os.path.exists(man_path), (
        "снимка схемы нет: database/baseline/baseline.sql и baseline.manifest")

    async def _recreate(name: str) -> None:
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {name}")
            await admin.execute(f"CREATE DATABASE {name}")
        finally:
            await admin.close()

    async def _boot(name: str, *, hide_baseline: bool):
        await _recreate(name)
        saved_dsn, saved_paths = _db.DATABASE_URL, _db.baseline_paths
        _db.DATABASE_URL = _dsn_for(name)
        if hide_baseline:
            _db.baseline_paths = lambda base_dir: (
                os.path.join(base_dir, "нет-снимка.sql"),
                os.path.join(base_dir, "нет-манифеста"))
        try:
            return await _db.create_pool()
        finally:
            _db.DATABASE_URL, _db.baseline_paths = saved_dsn, saved_paths

    snap = _run(_boot(DB_BASELINE, hide_baseline=False))
    hist = _run(_boot(DB_HISTORY, hide_baseline=True))
    try:
        yield snap, hist
    finally:
        _run(snap.close())
        _run(hist.close())


def test_clean_base_is_raised_by_the_snapshot(two_bases):
    """Чистая база встаёт снимком: в журнале снимок и все файлы манифеста,
    и ни одного файла с ошибкой."""
    snap, _hist = two_bases
    from database.db import BASELINE_JOURNAL_NAME, baseline_paths, parse_baseline_manifest

    _sql, man = baseline_paths(ROOT)
    with open(man, encoding="utf-8") as fh:
        manifest = parse_baseline_manifest(fh.read())
    rows = _run(snap.fetch("SELECT filename, status FROM schema_migrations"))
    journal = {r["filename"]: r["status"] for r in rows}
    assert journal.get(BASELINE_JOURNAL_NAME) == "ok", (
        "снимок не применился — база поднялась историей, а не снимком")
    missing = sorted(manifest - set(journal))
    assert not missing, f"файлы манифеста не помечены применёнными: {missing[:10]}"
    bad = sorted(k for k, v in journal.items() if v != "ok")
    assert not bad, f"после снимка файлы с ошибками: {bad[:10]}"


def test_history_base_did_not_take_the_snapshot(two_bases):
    """Контроль самого сравнения: вторая база поднята ИСТОРИЕЙ. Иначе обе
    стороны были бы снимком и совпадали бы при любом снимке."""
    _snap, hist = two_bases
    from database.db import BASELINE_JOURNAL_NAME

    n = _run(hist.fetchval(
        "SELECT count(*) FROM schema_migrations WHERE filename = $1",
        BASELINE_JOURNAL_NAME))
    assert n == 0


@pytest.mark.parametrize("kind", sorted(_CATALOG_QUERIES))
def test_snapshot_schema_equals_history_schema(two_bases, kind):
    snap, hist = two_bases
    a = _run(_catalog(snap))[kind]
    b = _run(_catalog(hist))[kind]
    only_snap = sorted(set(a) - set(b))
    only_hist = sorted(set(b) - set(a))
    differ = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    assert not (only_snap or only_hist or differ), (
        f"{kind}: снимок расходится с историей миграций.\n"
        f"  только в снимке: {only_snap[:10]}\n"
        f"  только в истории: {only_hist[:10]}\n"
        f"  различаются: {[(k, a[k], b[k]) for k in differ[:5]]}\n"
        "Снимок устарел или снят не с чистой базы: пересоздайте его — "
        "deploy/scripts/make_schema_baseline.py (docs/SCHEMA_CONSOLIDATION_PLAN.md).")

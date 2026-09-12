"""Дымовой тест старта: приложение обязано подниматься и отвечать.

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. В наборе было 3500+ тестов и НИ ОДНОГО, который
проверяет, что продукт вообще стартует. Из-за этого правка, сделанная по итогам
аудита, положила прод целиком: приложение перестало подниматься, а весь набор
оставался зелёным. Юнит-тесты проверяли детали внутри дома, у которого не
открывалась входная дверь.

Здесь проверяется САМАЯ ДЕШЁВАЯ И САМАЯ ВАЖНАЯ вещь — что цепочка
«импорт main → create_pool со всеми миграциями → сборка HTTP-приложения →
ответ на запрос» проходит целиком.

Нужен живой Postgres (INFRAGRAM_TEST_DSN): без него смысла нет — половина
падений старта именно на миграциях. Без переменной файл пропускается.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

pytest.importorskip("telethon", reason="старт тянет telethon")
pytest.importorskip("aiogram", reason="старт тянет aiogram")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _boot_dsn() -> str:
    """Отдельная база под старт: миграции накатываются с нуля, как на новом деплое.

    Имя БД — сегмент после последнего '/' (до '?' или конца строки). Раньше замена
    срабатывала ТОЛЬКО при наличии '?'-строки параметров: на DSN без неё
    (…/infragram_test) boot_smoke молча катал миграции в ОБЩУЮ базу, где другие
    тесты уже наплодили tg_accounts без строк в platform_users — и бэкфилл
    schema_v124 падал на FK. Теперь имя заменяется в обоих случаях."""
    import re
    return re.sub(r"/[^/?]+(\?|$)", r"/boot_smoke\1", DSN, count=1)


@pytest.fixture(scope="module")
def booted():
    """Полный путь старта: create_pool (все schema_v*.sql) + сборка HTTP-приложения."""
    import asyncpg
    from aiohttp import web

    async def _prepare():
        admin = await asyncpg.connect(DSN)
        try:
            await admin.execute("DROP DATABASE IF EXISTS boot_smoke")
            await admin.execute("CREATE DATABASE boot_smoke")
        finally:
            await admin.close()

    _run(_prepare())
    os.environ.setdefault("MANAGER_BOT_TOKEN", "1:test")
    os.environ.setdefault("TG_API_ID", "1")
    os.environ.setdefault("TG_API_HASH", "x")
    os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "t")

    # DATABASE_URL читается в config НА ИМПОРТЕ, поэтому переменной окружения
    # его уже не подменить — правим значение в самом модуле.
    from database import db as _db
    _saved_dsn = _db.DATABASE_URL
    _db.DATABASE_URL = _boot_dsn()
    try:
        pool = _run(_db.create_pool())
    finally:
        _db.DATABASE_URL = _saved_dsn

    from services import mini_app_api
    app = web.Application()
    mini_app_api.setup_routes(app, pool)

    yield pool, app
    _run(pool.close())


def test_imports_of_entrypoint_succeed():
    """main.py обязан импортироваться: сломанный импорт = процесс не стартует."""
    import main
    assert main._ROLE in ("all", "web", "worker")


def test_create_pool_applies_all_migrations(booted):
    """Старт проходит миграции целиком и не падает на них."""
    pool, _app = booted
    n = _run(pool.fetchval(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"))
    assert n > 100, f"схема почти пустая ({n} таблиц) — миграции не применились"
    bad = _run(pool.fetch(
        "SELECT filename FROM schema_migrations WHERE status <> 'ok' ORDER BY filename"))
    assert not bad, f"миграции с ошибками: {[r['filename'] for r in bad][:10]}"


def test_critical_tables_present_after_boot(booted):
    """Те самые таблицы, без которых операции и аудит молча проваливаются."""
    pool, _app = booted
    from database.db import _CRITICAL_TABLES
    for tbl in _CRITICAL_TABLES:
        exists = _run(pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=$1)", tbl))
        assert exists, f"после старта нет критичной таблицы {tbl}"


def test_account_claim_works_on_real_schema(booted):
    """Захват аккаунта обязан РАБОТАТЬ на боевой схеме.

    Он fail-closed: если колонки аренды не приехали, захват молча возвращает
    пустой список — и все операции встают, при этом ничего не падает. Именно
    такой отказ выглядит как «всё зависло», и поймать его можно только здесь.
    """
    pool, _app = booted
    from services import op_worker as ow

    # ВАЖНО: _db_pool — глобал модуля. Не восстановив его, мы оставляем другим
    # тестам ЗАКРЫТЫЙ пул, и они падают на «pool is closed» — так этот тест
    # однажды уронил 52 чужих. Сохраняем и возвращаем всё, что трогаем.
    _saved_pool = ow._db_pool
    _saved_in_use = set(ow._accounts_in_use)
    _saved_locks = dict(ow._operation_account_locks)
    ow.init_op_worker_pool(pool)

    async def go():
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=424242")
        ids = []
        for i in range(2):
            r = await pool.fetchrow(
                "INSERT INTO tg_accounts(owner_id, phone, session_str, is_active) "
                "VALUES($1,$2,'sess',TRUE) RETURNING id", 424242, f"+7900111{i}")
            ids.append(int(r["id"]))
        # 1) фоновый путь (призрак/прогрев)
        got = await ow.try_claim_accounts(ids)
        assert sorted(got) == sorted(ids), (
            "захват вернул пусто на боевой схеме — операции встанут молча")
        await ow.release_accounts(got)      # освобождаем, иначе следующий захват
                                            # ПРАВИЛЬНО откажет: аккаунты заняты
        # 2) путь операций
        claimed = await ow._claim_available_accounts(
            999001, [{"id": i} for i in ids], owner_id=424242)
        assert claimed, "операция не получила аккаунтов — операции встанут молча"
        await ow.release_accounts([int(a["id"]) for a in claimed])
        await pool.execute("DELETE FROM tg_accounts WHERE owner_id=424242")

    try:
        _run(go())
    finally:
        ow._db_pool = _saved_pool
        ow._accounts_in_use.clear(); ow._accounts_in_use.update(_saved_in_use)
        ow._operation_account_locks.clear()
        ow._operation_account_locks.update(_saved_locks)


def test_http_app_serves_miniapp_and_metrics(booted):
    """HTTP-приложение собирается и отвечает — иначе интерфейс мёртв."""
    from aiohttp.test_utils import TestClient, TestServer
    _pool, app = booted

    async def go():
        cli = TestClient(TestServer(app), auto_decompress=False)
        await cli.start_server()
        try:
            r = await cli.get("/miniapp", headers={"Accept-Encoding": "gzip"})
            assert r.status == 200, f"мини-апп не отдаётся: {r.status}"
            assert int(r.headers.get("Content-Length") or 0) > 1000

            m = await cli.get("/metrics")
            assert m.status == 200
            assert "infragram_" in await m.text()

            # неавторизованный API отвечает 401, а не падает 500
            a = await cli.get("/api/miniapp/accounts")
            assert a.status in (401, 403), f"ожидал отказ авторизации, получил {a.status}"
        finally:
            await cli.close()

    _run(go())


def test_background_services_are_gated_by_role_not_removed():
    """ROLE=web гасит фон, но НЕ ломает сборку: процесс всё равно поднимается.

    Проверяем на уровне исходника, потому что запускать 49 циклов в тесте
    нельзя, а именно этот гейт однажды и погасил всю фоновую работу в проде.
    """
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert 'os.getenv("INFRAGRAM_ROLE")' in src
    assert 'os.getenv("ROLE")' not in src, (
        "общее имя ROLE может совпасть с чужой переменной окружения и погасить фон")

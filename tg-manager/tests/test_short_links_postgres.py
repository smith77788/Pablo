"""Сокращатель ссылок: раздел был подключён целиком и не работал ни в одной части.

ЧТО БЫЛО. Эндпойнты мини-аппа создают, перечисляют, отключают и удаляют
короткую ссылку, публичный маршрут `/s/{code}` раздаёт их и считает клики. А
таблицу `short_links` не создавала ни одна миграция: её заводила только
`link_shortener.ensure_table`, которую НИКТО не вызывает. Каждый запрос падал
на несуществующей таблице, `except Exception` превращал падение в «данных нет»,
и раздел выглядел рабочим. Тот же случай, что с мёртвым разделом «Воркфлоу»
(tests/test_sql_tables_exist_in_schema.py — он это и ловит статически).

Здесь — живой Postgres: статическая проверка говорит «миграция есть», а эта
проходит по РЕАЛЬНОМУ пути раздела, от создания ссылки до подсчёта клика.
Без миграции на свежей базе падает первый же шаг.

Без INFRAGRAM_TEST_DSN — скип. Инструкция по стенду — в
tests/test_invite_e2e_postgres.py.
"""
from __future__ import annotations

import os

import pytest

asyncpg = pytest.importorskip("asyncpg")

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not _DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 553107
TARGET = "https://example.org/offer?a=1"


async def _pool():
    return await asyncpg.create_pool(_DSN, min_size=1, max_size=3,
                                     server_settings={"lock_timeout": "3000"})


async def _clean(pool):
    await pool.execute("DELETE FROM short_links WHERE owner_id=$1", OWNER)


@pytest.mark.asyncio
async def test_the_table_is_created_by_a_migration_not_by_the_module():
    pool = await _pool()
    try:
        assert await pool.fetchval(
            "SELECT to_regclass('public.short_links') IS NOT NULL"), (
            "таблицы нет: её заводит только ensure_table, которую никто не "
            "зовёт, и весь раздел сокращателя мёртв")
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_the_whole_section_works_from_creation_to_the_click():
    from services import link_shortener as lnk

    pool = await _pool()
    try:
        await _clean(pool)
        res = await lnk.create(pool, OWNER, TARGET, "Оффер")
        assert res and res.get("code"), f"ссылка не создана: {res}"
        code = res["code"]

        # Публичный редирект: отдаёт цель и считает клик.
        assert await lnk.resolve(pool, code) == TARGET
        clicks = await pool.fetchval(
            "SELECT clicks FROM short_links WHERE code=$1", code)
        assert clicks == 1, f"клик не посчитан: {clicks}"

        # Список владельца.
        rows = await lnk.list_for_owner(pool, OWNER)
        assert any(r["code"] == code for r in rows), rows

        # Отключение: ссылка больше не ведёт никуда.
        assert await lnk.set_disabled(pool, OWNER, code, True) is True
        assert await lnk.resolve(pool, code) is None, (
            "отключённая ссылка продолжает работать")

        assert await lnk.delete(pool, OWNER, code) is True
        assert await lnk.get_one(pool, OWNER, code) is None
    finally:
        await _clean(pool)
        await pool.close()


@pytest.mark.asyncio
async def test_another_owner_cannot_touch_the_link():
    """Скоуп по владельцу: код короткий и угадывается."""
    from services import link_shortener as lnk

    pool = await _pool()
    try:
        await _clean(pool)
        res = await lnk.create(pool, OWNER, TARGET, "Оффер")
        code = res["code"]
        assert await lnk.get_one(pool, OWNER + 1, code) is None
        assert await lnk.set_disabled(pool, OWNER + 1, code, True) is False
        assert await lnk.delete(pool, OWNER + 1, code) is False
        assert await lnk.resolve(pool, code) == TARGET, "ссылку погасил чужой"
    finally:
        await _clean(pool)
        await pool.close()

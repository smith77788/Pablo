"""topology_nodes должен брать каналы из таблицы, в которую реально пишут.

Баг (класс «читаем таблицу, которую никто не пишет»): topology_nodes читал каналы
из tg_channels — таблицы, в которую НЕТ ни одного INSERT/UPDATE в коде и сидов в
миграциях. Реальные каналы пользователя лежат в managed_channels (8 путей записи).
Итог: граф топологии НИКОГДА не показывал узлы-каналы. Фикс: читать из
managed_channels (owner_id-скоуп, колонки id/username/title есть).

Источниковый тест идёт в CI; функциональный — на живом Postgres (INFRAGRAM_TEST_DSN).
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _topology_fetch_sql() -> list[str]:
    """SQL-строки всех .fetch/.fetchrow/.fetchval внутри topology_nodes (AST
    склеивает соседние строковые литералы в один Constant)."""
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
               and n.name == "topology_nodes"), None)
    assert fn, "topology_nodes не найдена"
    out = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("fetch", "fetchrow", "fetchval")
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            out.append(node.args[0].value)
    return out


def test_topology_reads_channels_from_written_table():
    sqls = _topology_fetch_sql()
    joined = "\n".join(sqls)
    assert "managed_channels" in joined, (
        "topology_nodes должен читать каналы из managed_channels (туда пишут)")
    # ни один запрос топологии не читает из tg_channels (в неё никто не пишет)
    assert "tg_channels" not in joined, (
        "tg_channels никем не заполняется — узлы-каналы будут всегда пусты")


DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_topology_channels_query_valid_on_real_schema():
    """Точный SQL из topology_nodes валиден на боевой схеме и возвращает канал,
    засеянный в managed_channels."""
    import asyncio
    import asyncpg

    sql = next((s for s in _topology_fetch_sql() if "managed_channels" in s), None)
    assert sql, "не найден SELECT ... managed_channels в topology_nodes"

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=777042")
            await pool.execute(
                "INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username) "
                "VALUES (777042, 1, 6001, 'T', 'tchan')")
            rows = await pool.fetch(sql, 777042)
            assert any(r["id"] == 6001 for r in rows), "канал не вернулся из managed_channels"
            await pool.execute("DELETE FROM managed_channels WHERE owner_id=777042")
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())

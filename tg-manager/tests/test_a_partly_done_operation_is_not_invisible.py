"""Операция, доведённая не до конца, должна быть видна владельцу.

У слоя операций есть отдельный терминальный исход `partial` — «работа была, но
не доведена» (см. `services/op_status`). Он не формальность: исполнители
ставят его намеренно, когда часть целей сделана, а часть нет, вместо зелёного
«готово». За этот цикл его стало больше — таймаут синхронизации контактов,
сетевая рассылка с незакреплёнными ботами, развёртывание связки с
незапомненным узлом.

А два экрана, на которые владелец смотрит, считали только `done` и `failed`:
«✅ Завершено (24ч): N  ❌ Ошибок: M». Операция с исходом `partial` не попадала
НИ В ОДНО из этих чисел — то есть честный статус, поставленный ради владельца,
до владельца не доходил вовсе. Классический разрыв контракта: исход добавили,
а потребителей не обновили.

Проверяем оба экрана: и запрос (считает ли он `partial`), и текст (показывает
ли он его). Запрос вдобавок прогоняется на живой базе: текстовая проверка
доказывает, что строчка есть, а не что она считает правильно.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCREENS = (
    ("bot/handlers/channel_ops.py", "cmd_ops"),
    ("bot/handlers/start.py", None),
)


def _source(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _op_stats_sql(rel: str) -> str:
    """SQL сводки по operation_queue из исходника экрана."""
    src = _source(rel)
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        if "FROM operation_queue" in text and "AS running" in text:
            return text
    raise AssertionError(f"в {rel} не нашлась сводка по operation_queue")


@pytest.mark.parametrize("rel,_fn", _SCREENS)
def test_the_screen_counts_the_partly_done(rel, _fn):
    """Запрос сводки обязан считать исход partial отдельным числом."""
    sql = _op_stats_sql(rel)
    assert "partial" in sql, (
        f"{rel}: сводка считает только done и failed — операция с исходом "
        "'partial' не попадает ни в одно число, и владелец её не видит")


@pytest.mark.parametrize("rel,_fn", _SCREENS)
def test_the_screen_shows_the_partly_done(rel, _fn):
    """И показывает его словами, а не только считает."""
    src = _source(rel)
    assert "partial_24h" in src, (
        f"{rel}: число посчитано, но в текст не попало")
    assert "Частично" in src, (
        f"{rel}: исход показан без человеческого названия — владелец не "
        "поймёт, о чём число")


_DSN = os.getenv("INFRAGRAM_TEST_DSN")

pytestmark_db = pytest.mark.skipif(
    not _DSN, reason="нужна живая база: INFRAGRAM_TEST_DSN")

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not _DSN:
        pytest.skip("нужна живая база: INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        import pathlib

        p = await asyncpg.create_pool(_DSN, min_size=1, max_size=2)
        base = pathlib.Path(ROOT) / "database"
        files = [base / "schema.sql"] + sorted(base.glob("schema_v*.sql"))
        async with p.acquire() as conn:
            for f in files:
                if not f.exists():
                    continue
                try:
                    await conn.execute(f.read_text(encoding="utf-8"))
                except Exception:
                    pass
        return p

    p = _run(_mk())
    yield p
    _run(p.close())


@pytestmark_db
@pytest.mark.parametrize("rel,_fn", _SCREENS)
def test_the_query_really_counts_it_on_a_live_database(pool, rel, _fn):
    """Текст доказывает, что строчка есть; база — что она считает."""
    sql = _op_stats_sql(rel)
    owner = 918273645

    async def _go():
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM operation_queue WHERE owner_id=$1", owner)
            await conn.execute(
                "INSERT INTO operation_queue(owner_id, op_type, params, status, "
                "finished_at) VALUES($1,'contacts_sync','{}'::jsonb,'partial', now())",
                owner)
            await conn.execute(
                "INSERT INTO operation_queue(owner_id, op_type, params, status, "
                "finished_at) VALUES($1,'contacts_sync','{}'::jsonb,'done', now())",
                owner)
            try:
                row = await conn.fetchrow(sql, owner)
            finally:
                await conn.execute(
                    "DELETE FROM operation_queue WHERE owner_id=$1", owner)
        return dict(row or {})

    row = _run(_go())
    assert int(row.get("partial_24h") or 0) == 1, (
        f"{rel}: запрос не увидел операцию с исходом 'partial' — {row}")
    assert int(row.get("done_24h") or 0) == 1, (
        f"{rel}: заодно сломался счёт готовых — {row}")

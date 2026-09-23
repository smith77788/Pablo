"""Уборка очереди не стирает память о сделанном у живого повтора.

ЧТО БЫЛО БЫ. Журнал операции (operation_log) висит на строке очереди внешним
ключом ON DELETE CASCADE: удаление строки уносит журнал вместе с ней. А повтор —
это НОВАЯ операция, которая пропускает уже сделанное именно по журналу исходной
(op_worker.journal_op_ids).

Последовательность «повторить, а пока подчищу список» совершенно естественная, и
она стирала память о сделанном: повтор рассылки, вставшей на 203 адресатах из
380, уходил бы по всем 380 заново — то есть 203 человека получали бы второе
одинаковое сообщение. Ровно то, ради чего журнал и читается.

Два места уборки — кнопка мини-аппа (без ограничения по времени, поэтому она
могла снести предка сразу) и кнопка бота (старше суток).
"""
from __future__ import annotations

import inspect
import re

import pytest

from services import op_status


def _handler_src(module_src: str, name: str) -> str:
    m = re.search(
        r"\n(?:    )?async def " + name + r"\(.*?\n(.*?)\n(?:    )?async def ",
        module_src, re.DOTALL,
    )
    assert m, f"обработчик {name} не найден"
    return m.group(1)


# ── Список живых статусов — один на весь продукт ─────────────────────────────

def test_in_flight_list_covers_every_live_status():
    got = op_status.sql_in_flight_list()
    for st in op_status.IN_FLIGHT:
        assert f"'{st}'" in got, (
            f"статус «{st}» не попал в список живых: операция в нём будет "
            "выглядеть завершённой для уборки"
        )
    assert "'paused'" in got, (
        "приостановленная операция обязана считаться живой — иначе её "
        "предка снесёт уборка, пока владелец держит паузу"
    )
    for st in op_status.TERMINAL:
        assert f"'{st}'" not in got


def test_in_flight_and_terminal_lists_do_not_overlap():
    live = set(re.findall(r"'([a-z_]+)'", op_status.sql_in_flight_list()))
    dead = set(re.findall(r"'([a-z_]+)'", op_status.sql_terminal_list()))
    assert live and dead and not (live & dead)


# ── Обе кнопки уборки ────────────────────────────────────────────────────────

def test_miniapp_clear_keeps_the_ancestor_of_a_live_retry():
    from services import mini_app_api

    body = _handler_src(inspect.getsource(mini_app_api), "clear_operations")
    assert "DELETE FROM operation_queue" in body
    assert "retry_of_op" in body, (
        "«Очистить» снесёт операцию, на журнал которой опирается живой повтор — "
        "и повтор переделает всё, что исходная уже сделала"
    )
    assert "sql_in_flight_list()" in body, (
        "список живых статусов выписан руками — забытый «paused» вернёт тот же баг"
    )


def test_bot_clear_keeps_the_ancestor_of_a_live_retry():
    import bot.handlers.mass_ops as mass_ops

    src = inspect.getsource(mass_ops)
    start = src.index("async def cb_clear_completed")
    body = src[start:src.index("async def ", start + 10)]
    assert "retry_of_op" in body, (
        "кнопка бота сносит предка живого повтора (после суток ожидания)"
    )
    assert "sql_in_flight_list()" in body


# ── Поведение на живом Postgres ──────────────────────────────────────────────

import json  # noqa: E402
import os  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def _clear_sql() -> str:
    return (
        "DELETE FROM operation_queue oq "
        "WHERE oq.owner_id=$1 AND oq.status IN ('done','failed','cancelled') "
        "  AND NOT EXISTS (SELECT 1 FROM operation_queue r "
        "                   WHERE r.owner_id = oq.owner_id "
        f"                    AND r.status IN {op_status.sql_in_flight_list()} "
        "                     AND r.params->>'retry_of_op' = oq.id::text)"
    )


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
@pytest.mark.asyncio
async def test_clear_on_a_live_database():
    import asyncpg

    conn = await asyncpg.connect(DSN)
    owner = 990611
    try:
        await conn.execute("DELETE FROM operation_queue WHERE owner_id=$1", owner)

        async def _add(status, params="{}", finished=True):
            return await conn.fetchval(
                "INSERT INTO operation_queue(owner_id, op_type, status, params, "
                "created_at, finished_at) VALUES($1,'bulk_dm_adhoc',$2,$3::jsonb,now(),"
                "CASE WHEN $4 THEN now() ELSE NULL END) RETURNING id",
                owner, status, params, finished)

        ancestor = await _add("failed")
        plain = await _add("done")
        # Повтор кладёт ссылку ЧИСЛОМ — как mini_app_api и operation_bus.
        await _add("pending", json.dumps({"retry_of_op": ancestor}), finished=False)

        await conn.execute(_clear_sql(), owner)
        left = {r["id"] for r in await conn.fetch(
            "SELECT id FROM operation_queue WHERE owner_id=$1", owner)}

        assert ancestor in left, (
            "уборка снесла операцию, на журнал которой опирается живой повтор"
        )
        assert plain not in left, "уборка перестала убирать обычные завершённые"

        # Повтор закончился — предок больше никому не нужен.
        await conn.execute(
            "UPDATE operation_queue SET status='done', finished_at=now() "
            "WHERE owner_id=$1 AND status='pending'", owner)
        await conn.execute(_clear_sql(), owner)
        left = await conn.fetch("SELECT id FROM operation_queue WHERE owner_id=$1", owner)
        assert not left, "после повтора очередь так и не очищается"
    finally:
        await conn.execute("DELETE FROM operation_queue WHERE owner_id=$1", owner)
        await conn.close()

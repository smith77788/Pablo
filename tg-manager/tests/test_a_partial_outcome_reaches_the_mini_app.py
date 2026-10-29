"""Операция, закрытая «частично», исчезала из живого потока мини-аппа.

У слоя операций есть терминальный исход `partial` — «работа была, но не
доведена» (`services/op_status.py`, в терминальных у `operation_bus` он стоит
рядом с `done`/`failed`/`cancelled`/`skipped`). Сосед нашёл, что два экрана бота
считали только `done` и `failed`, и закрыл это в `afcafff`. В слое мини-аппа
остался третий потребитель, и он хуже тех двух: не счётчик, а СОБЫТИЕ.

`fetch_completed_ops` в SSE-потоке `/api/miniapp/events` выбирал
`status IN ('done','failed')`. Операция с исходом `partial` не попадала в поток
вовсе, а значит владелец не получал ни плашки итога, ни быстрых действий по
нему, ни перечитывания списка операций — операция просто исчезала из прогресса,
и итог доходил только в чат бота. Именно `partial` при этом и требует решения:
часть целей взята, часть нет.

Плашка исхода на неполном статусе уже вела себя правильно: `ok = status ===
'done'`, то есть ⚠️ вместо ✅ и кнопка «↺ Повторить» вместо следующего шага.
То есть не хватало ровно одного слова в списке статусов — а цена была в том,
что весь остальной путь уже был построен и не срабатывал.
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

from tests.miniapp_source import api_function, js_function

TERMINAL_IN_BUS = ("done", "partial", "failed", "cancelled", "skipped")


def test_the_live_stream_carries_a_partial_outcome():
    src = api_function("fetch_completed_ops")
    m = re.search(r"status IN \(([^)]*)\)", src)
    assert m, src[:400]
    statuses = {s.strip().strip("'\"") for s in m.group(1).split(",")}
    assert "partial" in statuses, (
        "поток итогов не берёт `partial`: недоведённая операция не даст ни "
        f"плашки, ни быстрых действий. Сейчас берутся: {sorted(statuses)}")
    assert {"done", "failed"} <= statuses, sorted(statuses)


def test_partial_is_terminal_in_the_bus():
    """Если бы `partial` не был терминальным, брать его в поток итогов значило
    бы объявлять законченной работу, которая ещё идёт."""
    import pathlib

    bus = (pathlib.Path(__file__).resolve().parents[1]
           / "services" / "operation_bus.py").read_text(encoding="utf-8")
    m = re.search(r"status IN \('done', 'partial'[^)]*\)", bus)
    assert m, "в operation_bus терминальные статусы перечислены иначе"
    for st in TERMINAL_IN_BUS:
        assert st in m.group(0), (st, m.group(0))


def test_the_banner_does_not_call_a_partial_outcome_a_success():
    js = js_function("showOpComplete")
    assert "op.status === 'done'" in js, (
        "плашка определяет успех иначе: неполный исход может получить ✅")
    assert "'✅' : '⚠️'" in js, js[:300]


def test_the_banner_offers_a_retry_on_a_partial_outcome():
    """Повтор уважает дедуп, поэтому на недоведённой операции он и нужен."""
    js = js_function("showOpComplete")
    assert "retryOp(" in js, "у неполного исхода нет кнопки повтора"
    can = js_function("opCanRetry")
    assert "'pending','running','paused','scheduled'" in can.replace(" ", ""), can[:200]
    assert "return true" in can, (
        "неизвестный или неполный статус запирает повтор")


def test_the_poller_stops_on_a_partial_outcome():
    """Опрос одиночной операции не должен врать «ещё выполняется» 60 секунд."""
    js = js_function("pollOpResult")
    assert "d.status==='partial'" in js.replace(" ", ""), (
        "опрос не останавливается на `partial`")


# ── Запрос прогоняется на живой базе ───────────────────────────────────────
#
# Проверка по тексту доказывает, что слово в списке есть, а не что запрос его
# считает: ошибка в условии (`finished_at` вместо `created_at`, лишний AND,
# не тот owner) текстом не видна. Приём соседа: вынуть запрос ПРЯМО из
# исходника экрана и прогнать на двух операциях — `partial` и `done`.

_DSN = os.getenv("INFRAGRAM_TEST_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="нужна живая база: INFRAGRAM_TEST_DSN")


def _completed_ops_sql() -> str:
    src = api_function("fetch_completed_ops")
    m = re.search(r'"""(\s*SELECT.*?)"""', src, re.DOTALL)
    assert m, "запрос итогов не найден в исходнике потока"
    return m.group(1)


@pytest.fixture
def _pool():
    import asyncpg

    loop = asyncio.new_event_loop()
    # asyncpg берёт ТЕКУЩИЙ луп, а не тот, в котором его зовут: без set_event_loop
    # пул рождается на одном лупе, а запрос уходит в другой.
    asyncio.set_event_loop(loop)
    p = loop.run_until_complete(asyncpg.create_pool(_DSN, min_size=1, max_size=2))
    try:
        yield loop, p
    finally:
        loop.run_until_complete(p.close())
        loop.close()
        asyncio.set_event_loop(None)


@_needs_db
def test_the_query_really_returns_a_partial_operation(_pool):
    loop, pool = _pool
    sql = _completed_ops_sql()
    owner = 918273646

    async def _go():
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM operation_queue WHERE owner_id=$1", owner)
            for st in ("partial", "done", "failed", "cancelled"):
                await conn.execute(
                    "INSERT INTO operation_queue(owner_id, op_type, params, status, "
                    "finished_at) VALUES($1,'contacts_sync','{}'::jsonb,$2, now())",
                    owner, st)
            try:
                rows = await conn.fetch(sql, owner)
            finally:
                await conn.execute(
                    "DELETE FROM operation_queue WHERE owner_id=$1", owner)
        return [dict(r) for r in rows]

    rows = loop.run_until_complete(_go())
    got = {r["status"] for r in rows}
    assert "partial" in got, (
        f"запрос не вернул операцию с исходом 'partial' — вернулись {sorted(got)}")
    assert {"done", "failed"} <= got, sorted(got)
    assert "cancelled" not in got, (
        "в поток итогов попала отменённая операция: по ней плашка «завершилась» "
        "не нужна, её закрыл сам владелец")


@_needs_db
def test_the_query_ignores_an_old_outcome(_pool):
    """Поток отдаёт итоги последних тридцати минут: иначе при каждом
    переподключении владелец получал бы плашки по всей истории."""
    loop, pool = _pool
    sql = _completed_ops_sql()
    owner = 918273647

    async def _go():
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM operation_queue WHERE owner_id=$1", owner)
            await conn.execute(
                "INSERT INTO operation_queue(owner_id, op_type, params, status, "
                "finished_at) VALUES($1,'contacts_sync','{}'::jsonb,'partial', "
                "now() - interval '2 hours')", owner)
            try:
                rows = await conn.fetch(sql, owner)
            finally:
                await conn.execute(
                    "DELETE FROM operation_queue WHERE owner_id=$1", owner)
        return rows

    assert loop.run_until_complete(_go()) == []

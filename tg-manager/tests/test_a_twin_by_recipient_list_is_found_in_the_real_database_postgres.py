"""Запрос о двойнике по списку получателей проверяется на живой базе.

ПОЧЕМУ ОТДЕЛЬНО ОТ СТЕНДА. Вторая половина запрета второго залпа
(`op_worker._exec_run_broadcast`) сравнивает не текст столбца, а МНОЖЕСТВА
получателей: `target_user_ids` — JSONB, и порядок id внутри списка ничем не
обещан. Такое сравнение целиком живёт в SQL (`jsonb_array_elements_text` →
`array_agg` → `= $5::bigint[]`), и стенд с подделанным пулом его не проверяет
вовсе: он повторяет сравнение руками. Ошибка в самом запросе не была бы
красной нигде, а в проде означала бы одно из двух — запрос не находит двойника
никогда (запрет мёртв, люди получают письмо дважды) или падает на каждом
залпе (все рассылки уходят в отложенные).

ЗАПРОС БЕРЁТСЯ ИЗ КОДА, А НЕ ПЕРЕПИСЫВАЕТСЯ ЗДЕСЬ. Копия в тесте зеленела бы,
даже если в обработчике запрос давно другой. Поэтому ниже из исходника
`services/op_worker.py` достаётся ровно та строка запроса, которую исполняет
обработчик, и выполняется на живой схеме.
"""
from __future__ import annotations

import ast
import asyncio
import os
import pathlib

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
ROOT = pathlib.Path(__file__).resolve().parent.parent
BOT = 7731990001
OWNER = 993618

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


def _queries() -> dict[str, str]:
    """Две половины запрета, достанутые из исходника обработчика."""
    src = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = {"subset": [], "all": []}
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        q = n.value
        if "FROM broadcasts" not in q or "status='running'" not in q:
            continue
        if "jsonb_typeof(target_user_ids)" in q:
            found["subset"].append(q)
        elif "target_user_ids IS NULL" in q:
            found["all"].append(q)
    return found


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    p = _run(_mk())
    _run(p.execute(
        "INSERT INTO managed_bots(token, bot_id, added_by) VALUES($1,$2,$3) "
        "ON CONFLICT (bot_id) DO NOTHING",
        f"{BOT}:test-twin-subset", BOT, OWNER))
    yield p
    _run(p.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT))
    _run(p.execute("DELETE FROM managed_bots WHERE bot_id=$1", BOT))
    _run(p.close())


def _new_broadcast(pool, text: str, targets: list[int] | None, status="running"):
    import json

    return _run(pool.fetchval(
        "INSERT INTO broadcasts(bot_id, message_text, status, created_by, "
        "                       target_user_ids) "
        "VALUES($1,$2,$3,$4,$5::jsonb) RETURNING id",
        BOT, text, status, OWNER,
        None if targets is None else json.dumps(targets)))


def test_both_halves_of_the_rule_are_in_the_source():
    # Пустой разбор сделал бы проверки ниже зелёными ни о чём.
    q = _queries()
    assert len(q["subset"]) == 1, q["subset"]
    assert len(q["all"]) == 1, q["all"]


def test_the_same_list_in_another_order_is_a_twin(pool):
    """Порядок id внутри списка ничем не обещан — сравниваются множества."""
    _run(pool.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT))
    live = _new_broadcast(pool, "Догонка", [1002, 1001])
    mine = _new_broadcast(pool, "Догонка", [1001, 1002], status="pending")

    row = _run(pool.fetchrow(_queries()["subset"][0],
                             BOT, mine, "Догонка", 3600, [1001, 1002]))
    assert row is not None and int(row["id"]) == live, (
        "двойник по тому же списку получателей не найден: повтор догонки "
        "напишет тем же людям во второй раз")


def test_a_different_list_is_not_a_twin(pool):
    """Иначе запрет сломал бы сегментные залпы и варианты A/B."""
    _run(pool.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT))
    _new_broadcast(pool, "Догонка", [1001, 1002])
    mine = _new_broadcast(pool, "Догонка", [1001], status="pending")

    row = _run(pool.fetchrow(_queries()["subset"][0],
                             BOT, mine, "Догонка", 3600, [1001]))
    assert row is None, "рассылку по другому списку приняли за двойника"


def test_the_two_halves_do_not_see_each_others_rows(pool):
    """Залп на всю аудиторию и залп по списку — разные рассылки, не двойники.

    Если бы половины видели строки друг друга, догонка недоставленным
    запрещалась бы живым залпом на всю аудиторию — а это штатный путь
    (`broadcaster.resend_undelivered`), и он идёт именно во время залпа.
    """
    _run(pool.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT))
    full = _new_broadcast(pool, "Анонс", None)
    mine = _new_broadcast(pool, "Анонс", [1001], status="pending")

    assert _run(pool.fetchrow(_queries()["subset"][0],
                              BOT, mine, "Анонс", 3600, [1001])) is None, (
        "догонку недоставленным запретил залп на всю аудиторию")

    sub = _new_broadcast(pool, "Анонс", [1001])
    assert _run(pool.fetchrow(_queries()["all"][0],
                              BOT, full, "Анонс", 3600)) is None, (
        f"залп на всю аудиторию запретила рассылка по списку (#{sub})")


def test_the_statute_of_limitations_applies_to_the_list_half(pool):
    """Срок давности нужен обеим половинам: мёртвая строка не запрещает навсегда."""
    _run(pool.execute("DELETE FROM broadcasts WHERE bot_id=$1", BOT))
    live = _new_broadcast(pool, "Старое", [1001])
    _run(pool.execute(
        "UPDATE broadcasts SET created_at = NOW() - INTERVAL '2 days' WHERE id=$1",
        live))
    mine = _new_broadcast(pool, "Старое", [1001], status="pending")

    row = _run(pool.fetchrow(_queries()["subset"][0],
                             BOT, mine, "Старое", 3600, [1001]))
    assert row is None, (
        "зависшая двухдневная строка запрещает отправку этого текста навсегда")

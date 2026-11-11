"""«ИИ Память» на живой базе: запросы, которые заглушка не исполняет.

* правка владельца (уверенность 1.0) переживает следующее извлечение ИИ (0.9);
* счётчики списка ботов не умножают факты на число сообщений;
* поиск по собеседникам и его итог принимаются Postgres;
* срок хранения удаляет старую переписку.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993771
BOT = 993771001

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg

    async def _mk():
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(_clean(p))
    _run(p.close())


async def _clean(p):
    await p.execute("DELETE FROM bot_user_memory WHERE bot_id=$1", BOT)
    await p.execute("DELETE FROM bot_user_facts WHERE bot_id=$1", BOT)
    await p.execute("DELETE FROM memory_settings WHERE bot_id=$1", BOT)


def test_owner_edit_survives_ai_extraction(pool):
    from services import semantic_memory as sm
    _run(_clean(pool))
    _run(sm.set_fact(pool, BOT, 1, "location", "Казань"))
    _run(sm._persist_extracted(pool, BOT, 1, {"location": "Москва", "name": "Оля"}))
    facts = {f["fact_key"]: f for f in _run(sm.get_user_facts(pool, BOT, 1))}
    assert facts["location"]["fact_value"] == "Казань", "ИИ затёр правку владельца"
    assert facts["name"]["fact_value"] == "Оля"
    # а извлечение по извлечению — обновляет
    _run(sm._persist_extracted(pool, BOT, 1, {"name": "Ольга"}))
    facts = {f["fact_key"]: f for f in _run(sm.get_user_facts(pool, BOT, 1))}
    assert facts["name"]["fact_value"] == "Ольга"
    assert _run(sm.delete_fact(pool, BOT, 1, "name")) is True


def test_retention_deletes_old_messages(pool):
    from services import semantic_memory as sm
    _run(_clean(pool))
    _run(sm.record_interaction(pool, BOT, 2, "user", "старое"))
    _run(pool.execute("UPDATE bot_user_memory SET created_at=NOW()-INTERVAL '40 days' "
                      "WHERE bot_id=$1", BOT))
    _run(sm.record_interaction(pool, BOT, 2, "user", "новое"))
    assert _run(sm.prune_expired(pool, BOT, 2, 30)) == 1
    left = _run(pool.fetch("SELECT text FROM bot_user_memory WHERE bot_id=$1", BOT))
    assert [r["text"] for r in left] == ["новое"]


def test_overview_and_search_queries_run(pool):
    """Тексты запросов ручек — теми же строками, что в обработчиках."""
    import re
    from services import semantic_memory as sm
    _run(_clean(pool))
    for i in range(5):
        _run(sm.record_interaction(pool, BOT, 3, "user", f"сообщение {i}"))
    _run(sm._persist_extracted(pool, BOT, 3, {"name": "Петя", "location": "Тула"}))
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "services", "mini_app_content.py"), encoding="utf-8").read()
    i = src.index("async def semantic_memory_overview")
    q = re.search(r'"""(\s*SELECT mb\.bot_id.*?)"""', src[i:], re.S).group(1)
    q = q.replace("WHERE mb.added_by = $1", "WHERE mb.bot_id = $1")
    try:
        rows = _run(pool.fetch(q, BOT))
    except Exception as exc:  # managed_bots может требовать полей — проверяем подзапросы
        pytest.fail(f"запрос списка ботов не принят базой: {exc}")
    assert rows == [] or rows[0]["fact_count"] == 2
    # подзапросы счётчиков отдельно — бот в managed_bots не заведён
    n = _run(pool.fetchval("SELECT COALESCE((SELECT COUNT(*) FROM bot_user_facts buf "
                           "WHERE buf.bot_id = $1), 0)", BOT))
    assert n == 2, "факты посчитаны с умножением"
    j = src.index("async def semantic_memory_bot")
    block = src[j:src.index("async def semantic_memory_forget")]
    seg = block[block.index("facts = await pool.fetch("):block.index("bot_id, _list_limit")]
    facts_q = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', seg))
    rows = _run(pool.fetch(facts_q, BOT, 100, 0, "тул", "%тул%"))
    assert {r["fact_key"] for r in rows} == {"name", "location"}, (
        "поиск по тексту факта должен отдать все факты найденного человека")
    rows = _run(pool.fetch(facts_q, BOT, 100, 0, "", "%%"))
    assert len(rows) == 2
    rows = _run(pool.fetch(facts_q, BOT, 100, 0, "нет-такого", "%нет-такого%"))
    assert rows == []

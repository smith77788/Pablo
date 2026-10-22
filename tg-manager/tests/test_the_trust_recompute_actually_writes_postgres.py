"""Пересчёт доверия действительно записывает баллы в базу.

РАЗРЫВ. `update_trust_scores` пишет баллы одной пачкой, и пачка собиралась так:

    unnest($1::bigint[], $2::double precision[]) ... WHERE u.id = v.id

а `unified_contacts.id` — UUID. Postgres отбивал запрос ЦЕЛИКОМ («operator
does not exist: uuid = bigint»), то есть пересчёт не записывал ничего и при
этом возвращал число обработанных контактов: владелец нажимал «пересчитать»,
видел «обработано N» и нули в баллах. Проверки на заглушках такого не ловят —
они видят текст запроса, а не ответ Postgres.

ЭТОТ ТЕСТ идёт в живую базу: после пересчёта балл в строке обязан отличаться
от исходного нуля. Классовую проверку (любой запрос проекта обязан быть
принят Postgres) держит `test_sql_runs_on_real_schema_postgres`.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993431

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
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    yield p
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(p.close())


def test_the_recompute_writes_the_score(pool):
    from services.contacts_hub import trust_engine

    cid = _run(pool.fetchval(
        """INSERT INTO unified_contacts(owner_id, telegram_user_id, username,
                                        first_name, phones, trust_score)
           VALUES($1, 4242, 'someone', 'Кто-то', '["+70001112233"]'::jsonb, 0)
        RETURNING id""",
        OWNER,
    ))
    assert cid is not None

    n = _run(trust_engine.update_trust_scores(pool, OWNER))
    assert n == 1, f"пересчёт сказал «обработано {n}» вместо одного"

    score = _run(pool.fetchval(
        "SELECT trust_score FROM unified_contacts WHERE id=$1", cid))
    assert score and float(score) > 0, (
        "пересчёт вернул число обработанных, но балл в базе не изменился — "
        "запись пачкой не доехала")


def test_the_recompute_does_not_touch_another_owner(pool):
    from services.contacts_hub import trust_engine

    other = OWNER + 1
    _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", other))
    cid = _run(pool.fetchval(
        """INSERT INTO unified_contacts(owner_id, telegram_user_id, username,
                                        first_name, phones, trust_score)
           VALUES($1, 4343, 'alien', 'Чужой', '["+70009998877"]'::jsonb, 0)
        RETURNING id""",
        other,
    ))
    try:
        _run(trust_engine.update_trust_scores(pool, OWNER))
        score = _run(pool.fetchval(
            "SELECT trust_score FROM unified_contacts WHERE id=$1", cid))
        assert float(score or 0) == 0, "пересчёт тронул контакт чужого владельца"
    finally:
        _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", other))

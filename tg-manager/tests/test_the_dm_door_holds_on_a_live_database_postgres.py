"""Дверь дедупа ЛС проверяется на живом Postgres, а не на заглушке.

Предзапись «отправлено» ДО отправки держится ровно на том, что обещает
Postgres: частичный уникальный индекс `idx_dm_log_dedup (campaign_id,
tg_user_id) WHERE status='sent'`. На заглушке проверка подтверждает форму
кода, а не поведение — ON CONFLICT DO NOTHING на стенде «срабатывает»
всегда, потому что стенд ничего не хранит.

Что здесь доказывается на самой базе:

  * вторая предзапись той же цели в той же кампании НЕ создаёт второй строки
    (иначе повторный прогон завёл бы человеку два «отправлено», а счёт по
    статусам кампании поехал бы);
  * `_settle_dm_target` переводит предзапись в реальный исход ОДНИМ запросом
    и освобождает место в частичном индексе, то есть исход не раздваивается;
  * `_unmark_dm_target` снимает только предзапись и возвращает человека в
    кампанию: запрос аудитории снова его видит;
  * честный исход ('blocked', 'skip') человека в кампанию НЕ возвращает.

Рецепт запуска — в docstring tests/test_op_finish_paths_e2e_postgres.py.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 889922
TARGET = 70001

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

    async def _boot():
        conn = await asyncpg.connect(DSN)
        for f in ["schema.sql"] + sorted(
                glob.glob("schema_v*.sql"),
                key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1))):
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass          # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture
def campaign(pool):
    """Своя кампания на каждый тест: журнал уезжает по ON DELETE CASCADE."""
    async def _mk():
        await pool.execute(
            "DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER)
        return int(await pool.fetchval(
            "INSERT INTO dm_campaigns(owner_id, name, text_template, status) "
            "VALUES($1,'стенд двери','Привет','running') RETURNING id", OWNER))

    cid = _run(_mk())
    yield cid
    _run(pool.execute("DELETE FROM dm_campaigns WHERE owner_id=$1", OWNER))


async def _rows(pool, cid):
    return [dict(r) for r in await pool.fetch(
        "SELECT status, account_id FROM dm_campaign_log WHERE campaign_id=$1 "
        "ORDER BY id", cid)]


async def _excluded(pool, cid) -> bool:
    """Так кампания считает уже обработанных: тот же набор статусов."""
    return bool(await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM dm_campaign_log "
        " WHERE campaign_id=$1 AND tg_user_id=$2 "
        "   AND status IN ('sent','blocked','skip','unconfirmed'))", cid, TARGET))


def test_the_second_premark_does_not_create_a_second_row(pool, campaign):
    from services import dm_engine

    assert _run(dm_engine._premark_dm_target(pool, campaign, 11, TARGET)) is True
    assert _run(dm_engine._premark_dm_target(pool, campaign, 12, TARGET)) is True
    rows = _run(_rows(pool, campaign))
    assert len(rows) == 1, (
        f"в журнале {len(rows)} строк на одну цель: частичный уникальный индекс "
        "не держит, и повторный прогон задвоил бы человеку «отправлено»")
    assert rows[0]["status"] == "sent"
    assert _run(_excluded(pool, campaign)) is True


def test_an_outcome_replaces_the_premark_instead_of_doubling_it(pool, campaign):
    from services import dm_engine

    _run(dm_engine._premark_dm_target(pool, campaign, 11, TARGET))
    _run(dm_engine._settle_dm_target(pool, campaign, 11, TARGET,
                                     "blocked", "закрыл личку"))
    rows = _run(_rows(pool, campaign))
    assert len(rows) == 1 and rows[0]["status"] == "blocked", rows
    # Место в частичном индексе освободилось — но цель всё равно исключена,
    # потому что 'blocked' это окончательный отказ, а не наш сбой.
    assert _run(dm_engine._premark_dm_target(pool, campaign, 11, TARGET)) is True
    assert len(_run(_rows(pool, campaign))) == 2, (
        "после перевода исхода предзапись не ставится — значит индекс считает "
        "место занятым, и повтор кампании не смог бы написать никому")
    assert _run(_excluded(pool, campaign)) is True


def test_an_untried_target_comes_back_into_the_campaign(pool, campaign):
    from services import dm_engine

    _run(dm_engine._premark_dm_target(pool, campaign, 11, TARGET))
    assert _run(_excluded(pool, campaign)) is True
    _run(dm_engine._unmark_dm_target(pool, campaign, TARGET))
    assert _run(_rows(pool, campaign)) == [], (
        "предзапись не снялась: человек выпал из кампании навсегда, так и не "
        "получив сообщения")
    assert _run(_excluded(pool, campaign)) is False


def test_an_honest_outcome_is_never_lifted_by_the_unmark(pool, campaign):
    """Снимать можно только предзапись, иначе терялись бы реальные исходы."""
    from services import dm_engine

    _run(pool.execute(
        "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status) "
        "VALUES($1,11,$2,'skip')", campaign, TARGET))
    _run(dm_engine._unmark_dm_target(pool, campaign, TARGET))
    rows = _run(_rows(pool, campaign))
    assert len(rows) == 1 and rows[0]["status"] == "skip", rows
    assert _run(_excluded(pool, campaign)) is True


def test_an_outcome_without_a_premark_is_still_recorded(pool, campaign):
    """Предзапись могли потерять; исход цели обязан попасть в журнал."""
    from services import dm_engine

    _run(dm_engine._settle_dm_target(pool, campaign, 11, TARGET, "skip", "удалён"))
    rows = _run(_rows(pool, campaign))
    assert len(rows) == 1 and rows[0]["status"] == "skip", rows

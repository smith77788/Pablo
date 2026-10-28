"""Воронка считает то, что лежит в базе, и только у своего владельца.

Пара к tests/test_crm_funnel_shows_numbers.py: там проверяется, что число на
чипе берётся с сервера одним кодом, здесь — что это число верное. Текст
запроса тут не годится: вопрос ровно в том, что вернёт GROUP BY на реальных
строках, с пустыми стадиями, с разными валютами и с чужими строками рядом.

ОБРАТНЫЕ КОНТРОЛИ (прогнаны). (1) Собирать ответ только из того, что вернул
GROUP BY (без предзаполнения по STAGE_LABELS) — краснеет
test_every_stage_is_in_the_answer_even_when_empty: воронка приходит с дырой
в середине. (2) Складывать все валюты в одну сумму
(`cur = 'ИТОГО'`) — краснеет test_sums_are_split_by_currency. (3) Снять
ограничение по владельцу в запросе — краснеет
test_a_stranger_sees_only_their_own_funnel. (Снятие самого `GROUP BY stage,
currency` краснит пять проверок из шести: запрос становится невалидным,
Postgres отбивает его целиком.)

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py; переменная
та же, INFRAGRAM_TEST_DSN.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991850
STRANGER = 991851
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

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda f: int(re.search(r"schema_v(\d+)", f).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(_wipe(p))
    _run(p.close())


async def _wipe(p):
    for uid in (OWNER, STRANGER):
        await p.execute("DELETE FROM contact_crm WHERE owner_id=$1", uid)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", uid)


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(_wipe(pool))
    yield


def _contact(pool, owner, name="Иван"):
    return _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name) VALUES ($1,$2) "
        "RETURNING id", owner, name))

def _contact_with_crm(pool, owner, stage, value=0, currency="RUB"):
    cid = _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name) VALUES ($1,'Иван') "
        "RETURNING id", owner))
    _run(pool.execute(
        "INSERT INTO contact_crm (owner_id, contact_id, stage, deal_value, currency) "
        "VALUES ($1,$2,$3,$4,$5)", owner, cid, stage, value, currency))
    return cid


def test_the_funnel_counts_what_is_in_the_base(pool):
    from services.contacts_hub.crm_engine import pipeline

    _contact_with_crm(pool, OWNER, "lead")
    _contact_with_crm(pool, OWNER, "lead")
    _contact_with_crm(pool, OWNER, "won", 1000)
    d = _run(pipeline(pool, OWNER))
    by = {s["stage"]: s for s in d["stages"]}
    assert by["lead"]["count"] == 2
    assert by["won"]["count"] == 1
    assert d["total"] == 3


def test_every_stage_is_in_the_answer_even_when_empty(pool):
    """Воронка с пропущенной серединой читается как «там никого нет и не было».

    Разница важна: «ноль в Предложении» — это сигнал, а отсутствие строки —
    это ничего не говорит.
    """
    from services.contacts_hub.crm_engine import STAGE_LABELS, pipeline

    _contact_with_crm(pool, OWNER, "lead")
    d = _run(pipeline(pool, OWNER))
    assert [s["stage"] for s in d["stages"]] == list(STAGE_LABELS), (
        "в ответе не все стадии или не в порядке воронки")
    assert all(s["label"] for s in d["stages"]), "стадия без русской подписи"


def test_sums_are_split_by_currency(pool):
    """Рубли с долларами в одном «итого» — красивое и неверное число."""
    from services.contacts_hub.crm_engine import pipeline

    _contact_with_crm(pool, OWNER, "proposal", 1000, "RUB")
    _contact_with_crm(pool, OWNER, "proposal", 50, "USD")
    d = _run(pipeline(pool, OWNER))
    prop = {s["stage"]: s for s in d["stages"]}["proposal"]
    assert prop["count"] == 2
    assert prop["by_currency"] == {"RUB": 1000.0, "USD": 50.0}


def test_a_stranger_sees_only_their_own_funnel(pool):
    from services.contacts_hub.crm_engine import pipeline

    _contact_with_crm(pool, OWNER, "lead")
    _contact_with_crm(pool, OWNER, "won", 999)
    d = _run(pipeline(pool, STRANGER))
    assert d["total"] == 0, "чужая воронка посчитала мои сделки"


def test_an_unknown_stage_is_shown_not_hidden(pool):
    """Старые строки со стадией вне списка не должны исчезать из воронки.

    Контакт, которого нет ни в одной стадии, но который есть в общем счёте, —
    это расхождение, которое владелец не сможет объяснить.
    """
    from services.contacts_hub.crm_engine import pipeline

    _contact_with_crm(pool, OWNER, "prospect")
    d = _run(pipeline(pool, OWNER))
    extra = [s for s in d["stages"] if s["stage"] == "prospect"]
    assert extra and extra[0]["count"] == 1, "строка со старой стадией пропала"
    assert d["total"] == 1


def test_an_unknown_stage_cannot_be_written_anymore(pool):
    from services.contacts_hub.crm_engine import BadStage, upsert_crm

    cid = _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name) VALUES ($1,'Иван') "
        "RETURNING id", OWNER))
    with pytest.raises(BadStage):
        _run(upsert_crm(pool, OWNER, str(cid), {"stage": "prospect"}))
    rows = _run(pool.fetch("SELECT 1 FROM contact_crm WHERE owner_id=$1", OWNER))
    assert rows == [], "строка со стадией вне списка всё равно записалась"

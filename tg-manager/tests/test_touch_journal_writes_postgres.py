"""Касание действительно записывается — и только в свой контакт.

Пара к tests/test_touch_journal_has_a_door.py: там проверяется, что дверь
есть, здесь — что за дверью работает. Проверка текста запроса тут не годится
дважды: во-первых, запись идёт в таблицу, которой до этого круга не
пользовался никто, и её может просто не быть в схеме; во-вторых, подпись
касания считается в питоне, а вернуться она должна из того же чтения, что
рисует экран. Ровно так уже обожглись на пересчёте доверия: текст запроса
был безупречен, а Postgres отбивал его целиком.

ОБРАТНЫЕ КОНТРОЛИ (прогнаны). (1) Убрать `_require_contact` из
log_crm_activity — краснеет test_a_stranger_cannot_write_into_my_contact:
запись по чужому id проходит. (2) Вернуть чтение без подписи (`[dict(r) for r
in rows]`) — краснеют test_the_journal_comes_back_with_russian_labels и
test_an_unknown_type_is_still_shown_not_hidden.

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
OWNER = 991848
STRANGER = 991849
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
        await p.execute("DELETE FROM contact_crm_activity WHERE owner_id=$1", uid)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", uid)


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(_wipe(pool))
    yield


def _contact(pool, owner, name="Иван"):
    return _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name) VALUES ($1,$2) "
        "RETURNING id", owner, name))


def test_a_touch_is_stored_and_read_back(pool):
    from services.contacts_hub.crm_engine import get_crm_activity, log_crm_activity

    cid = _contact(pool, OWNER)
    _run(log_crm_activity(pool, OWNER, str(cid), "call", "созвонились о сроках"))
    rows = _run(get_crm_activity(pool, OWNER, str(cid)))
    assert len(rows) == 1, "касание не записалось"
    assert rows[0]["description"] == "созвонились о сроках"


def test_the_journal_comes_back_with_russian_labels(pool):
    """Экран показывает `activity_label`; без него в журнале стоит «call»."""
    from services.contacts_hub.crm_engine import get_crm_activity, log_crm_activity

    cid = _contact(pool, OWNER)
    _run(log_crm_activity(pool, OWNER, str(cid), "meeting", "встретились"))
    rows = _run(get_crm_activity(pool, OWNER, str(cid)))
    assert rows[0]["activity_label"] == "Встреча", (
        "журнал вернулся без русской подписи — владелец читал бы «meeting»")


def test_an_unknown_type_is_still_shown_not_hidden(pool):
    """Старые строки с неизвестным типом не должны исчезать из журнала."""
    from services.contacts_hub.crm_engine import get_crm_activity

    cid = _contact(pool, OWNER)
    _run(pool.execute(
        "INSERT INTO contact_crm_activity (owner_id, contact_id, activity_type, "
        "description) VALUES ($1,$2,'legacy_ping','из прошлой жизни')",
        OWNER, cid))
    rows = _run(get_crm_activity(pool, OWNER, str(cid)))
    assert len(rows) == 1
    assert rows[0]["activity_label"] == "legacy_ping", (
        "пустая подпись вместо незнакомого типа — строка выглядит сломанной")


def test_a_stranger_cannot_write_into_my_contact(pool):
    from services.contacts_hub.crm_engine import (
        ContactNotFound, get_crm_activity, log_crm_activity,
    )

    cid = _contact(pool, OWNER)
    with pytest.raises(ContactNotFound):
        _run(log_crm_activity(pool, STRANGER, str(cid), "note", "чужая запись"))
    assert _run(get_crm_activity(pool, OWNER, str(cid))) == [], (
        "чужой дописал в журнал моего контакта")


def test_the_journal_does_not_leak_to_a_stranger(pool):
    from services.contacts_hub.crm_engine import get_crm_activity, log_crm_activity

    cid = _contact(pool, OWNER)
    _run(log_crm_activity(pool, OWNER, str(cid), "deal", "подписали"))
    assert _run(get_crm_activity(pool, STRANGER, str(cid))) == [], (
        "журнал чужого контакта читается по id")

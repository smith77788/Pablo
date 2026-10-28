"""Контакт заводится и повторным вызовом не двоится.

ЧТО БЫЛО. upsert_contact писал `ON CONFLICT (owner_id, telegram_user_id)`, а
такого ограничения в схеме нет — только обычный, НЕ уникальный индекс. Postgres
проверяет это на ИСПОЛНЕНИИ, поэтому запрос спокойно проходил разбор и падал
каждый раз, когда его звали. Единственный вызывающий — сенсор намерений: он не
мог завести ни одного контакта, а значит и состояние воронки новому человеку
писать было некуда.

Заглушка пула этого не видит в принципе: она не исполняет SQL. Отсюда живой
Postgres.

КАК ЗАПУСТИТЬ — см. докстринг tests/test_invite_e2e_postgres.py; переменная та
же, INFRAGRAM_TEST_DSN.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991801
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
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER))
    yield


def test_contact_is_created_and_not_duplicated(pool):
    from services.contacts_hub.repository import upsert_contact

    now = dt.datetime.now(dt.timezone.utc)
    first = _run(upsert_contact(pool, OWNER, {
        "telegram_user_id": 424242, "first_name": "Аня", "discovered_at": now}))
    assert first, "контакт не создан — ровно на этом падал сенсор намерений"

    again = _run(upsert_contact(pool, OWNER, {
        "telegram_user_id": 424242, "username": "anya", "discovered_at": now}))
    assert again == first, "повторный вызов завёл второй контакт тому же человеку"

    count = _run(pool.fetchval(
        "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1", OWNER))
    assert count == 1, f"строк {count} вместо одной"


def test_second_call_fills_gaps_and_keeps_what_was(pool):
    from services.contacts_hub.repository import upsert_contact

    now = dt.datetime.now(dt.timezone.utc)
    cid = _run(upsert_contact(pool, OWNER, {
        "telegram_user_id": 424243, "first_name": "Пётр", "discovered_at": now}))
    _run(upsert_contact(pool, OWNER, {
        "telegram_user_id": 424243, "username": "petr", "discovered_at": now}))

    row = _run(pool.fetchrow(
        "SELECT first_name, username FROM unified_contacts WHERE id=$1", cid))
    assert row["username"] == "petr", "новое поле не записалось"
    assert row["first_name"] == "Пётр", (
        "имя затёрто пустым значением: COALESCE в UPDATE перестал защищать")


def test_contacts_of_other_owners_are_untouched(pool):
    from services.contacts_hub.repository import upsert_contact

    now = dt.datetime.now(dt.timezone.utc)
    other = OWNER + 1
    _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", other))
    try:
        mine = _run(upsert_contact(pool, OWNER, {
            "telegram_user_id": 424244, "first_name": "Моя", "discovered_at": now}))
        theirs = _run(upsert_contact(pool, other, {
            "telegram_user_id": 424244, "first_name": "Чужая", "discovered_at": now}))
        assert mine != theirs, (
            "один и тот же человек у двух владельцев склеился в один контакт")
    finally:
        _run(pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", other))


def test_intent_sensor_can_create_a_contact(pool):
    """Сквозной смысл фикса: сенсор намерений заводит контакт."""
    from services.intent_sensor import _ensure_contact

    cid = _run(_ensure_contact(pool, OWNER, {
        "peer_user_id": 424245, "peer_name": "Новый",
        "peer_username": "newbie"}))
    assert cid, "сенсор намерений снова не может завести контакт"
    again = _run(_ensure_contact(pool, OWNER, {"peer_user_id": 424245}))
    assert again == cid

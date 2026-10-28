"""Группу контакта меняет только её владелец — проверка на живой базе.

НАХОДКА (22.10.2026). Ручки POST/DELETE
/api/miniapp/uch/contacts/{contact_id}/groups/{group_id} брали id контакта и
id группы из адреса и шли прямо в
repository.add_contact_to_group(pool, contact_id, group_id) — функцию, в
запросе которой владельца не было ВООБЩЕ. Авторизация проверяла только то,
что запрос пришёл от живого пользователя мини-аппа. Дальше любой такой
пользователь мог положить свой контакт в ЧУЖУЮ группу и, что хуже, вынуть
чужой контакт из чужой группы: id группы — обычное целое, его не надо
угадывать, достаточно перебрать.

Пакетный путь (bulk_ops_engine.bulk_add_to_group) эту же дыру уже закрыл —
там владельца проверяет сам запрос, и в коде об этом есть комментарий.
Одиночный путь остался как был: одна ошибка, две копии, починена одна. Поэтому
вторая копия удалена, а не исправлена: карточка контакта зовёт тот же
массовый путь списком из одного контакта (см. cdEditGroups и
tests/test_contact_groups_reachable.py). Здесь проверяется ровно он — на
списке из одного контакта, как его зовёт карточка.

ПОЧЕМУ ЖИВОЙ POSTGRES. Вопрос здесь не в тексте запроса, а в том, что
осталось в contact_group_members после чужой попытки. Проверка на заглушке
смотрела бы на текст — ровно так уже обожглись на пересчёте доверия
(см. tests/test_contact_trust_is_actually_computed.py): текст был
безупречен, а Postgres отбивал запрос целиком.

ОБРАТНЫЕ КОНТРОЛИ (прогнаны, а не предположены). (1) Снять в
bulk_add_to_group условие `c.owner_id = g.owner_id` — краснеет
test_a_stranger_cannot_put_my_contact_in_their_group. (2) Снять в
bulk_remove_from_group условие `g.owner_id = $2` — краснеет
test_a_stranger_cannot_empty_my_group. (3) Снять в добавлении ОБА условия —
краснеет и test_a_stranger_cannot_fill_my_group.

Третий контроль показывает устройство защиты, и это важнее, чем кажется:
в ДОБАВЛЕНИИ чужую пару отбивает join `c.owner_id = g.owner_id`, а не
`g.owner_id = $2`, — снятое по одному, условие владельца группы не краснит
ни одной проверки. То есть «упростить» join здесь нельзя: он и есть замок,
а `g.owner_id = $2` — второй контур. В УДАЛЕНИИ join'а нет, и там замок
ровно один, `g.owner_id = $2`.

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
OWNER = 991846
STRANGER = 991847
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
        await p.execute("DELETE FROM contact_groups WHERE owner_id=$1", uid)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id=$1", uid)


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(_wipe(pool))
    yield


def _contact(pool, owner, name):
    return _run(pool.fetchval(
        "INSERT INTO unified_contacts (owner_id, first_name) VALUES ($1, $2) "
        "RETURNING id", owner, name))


def _group(pool, owner, name):
    return _run(pool.fetchval(
        "INSERT INTO contact_groups (owner_id, name) VALUES ($1, $2) RETURNING id",
        owner, name))


def _members(pool, gid):
    rows = _run(pool.fetch(
        "SELECT contact_id FROM contact_group_members WHERE group_id=$1", gid))
    return {str(r["contact_id"]) for r in rows}


# ── Своё ─────────────────────────────────────────────────────────────────────

def _add(pool, cid, gid, owner):
    """Так же, как карточка контакта: массовая ручка списком из одного."""
    from services.contacts_hub.bulk_ops_engine import bulk_add_to_group
    return _run(bulk_add_to_group(pool, owner, [str(cid)], gid))


def _remove(pool, cid, gid, owner):
    from services.contacts_hub.bulk_ops_engine import bulk_remove_from_group
    return _run(bulk_remove_from_group(pool, owner, [str(cid)], gid))


def test_the_owner_puts_a_contact_in_a_group(pool):
    cid = _contact(pool, OWNER, "Иван")
    gid = _group(pool, OWNER, "Клиенты")
    assert _add(pool, cid, gid, OWNER) == {'added': 1}
    assert _members(pool, gid) == {str(cid)}


def test_adding_the_same_contact_twice_changes_nothing_and_says_so(pool):
    """Повтор отвечает «добавлено 0» — и карточка пишет «уже в этой группе».

    Счётчик считает СТРОКИ, а не попытки: ответ «добавлено 1» на повторе был
    бы обещанием работы, которой не было.
    """
    cid = _contact(pool, OWNER, "Иван")
    gid = _group(pool, OWNER, "Клиенты")
    _add(pool, cid, gid, OWNER)
    assert _add(pool, cid, gid, OWNER) == {'added': 0}
    assert _members(pool, gid) == {str(cid)}


def test_the_owner_removes_a_contact_from_a_group(pool):
    cid = _contact(pool, OWNER, "Иван")
    gid = _group(pool, OWNER, "Клиенты")
    _add(pool, cid, gid, OWNER)
    assert _remove(pool, cid, gid, OWNER) == {'removed': 1}
    assert _members(pool, gid) == set()


# ── Чужое ────────────────────────────────────────────────────────────────────

def test_a_stranger_cannot_fill_my_group(pool):
    gid = _group(pool, OWNER, "Клиенты")
    their = _contact(pool, STRANGER, "Чужой")
    assert _add(pool, their, gid, STRANGER) == {'added': 0}
    assert _members(pool, gid) == set(), (
        "чужой контакт лёг в мою группу: id группы — обычное целое, его "
        "перебирают")


def test_a_stranger_cannot_put_my_contact_in_their_group(pool):
    mine = _contact(pool, OWNER, "Иван")
    theirs = _group(pool, STRANGER, "Их список")
    assert _add(pool, mine, theirs, STRANGER) == {'added': 0}
    assert _members(pool, theirs) == set(), (
        "мой человек оказался в чужом списке рассылки")


def test_a_stranger_cannot_empty_my_group(pool):
    cid = _contact(pool, OWNER, "Иван")
    gid = _group(pool, OWNER, "Клиенты")
    _add(pool, cid, gid, OWNER)
    assert _remove(pool, cid, gid, STRANGER) == {'removed': 0}
    assert _members(pool, gid) == {str(cid)}, "чужой вынул контакт из моей группы"

"""Инвайт, прерванный деплоем, не позовёт человека второй раз.

ПОЧЕМУ ИМЕННО ЭТО. Инвайт — самая баноопасная операция продукта, а перезапуск
процесса — штатное событие каждого деплоя (ветка едет на Railway, пуш в неё
означает прод). Повтор операции поднимает исполнителя заново и ведёт по всему
списку сначала, поэтому между прогонами человека держит не память процесса, а
журнал приглашений `invite_target_log` — и только он.

Журнал устроен тоньше, чем кажется, и каждая тонкость уже стоила повторного
приглашения:

  * один канал приходит в разные двери по-разному — @username, ссылкой, числовым
    id, — поэтому запись идёт под ВСЕМИ формами, а чтение по любой;
  * `-100…` у канала и голый id — одно и то же;
  * '@Ivan' и '@ivan' — один человек, и Telegram матчит username именно так;
    сырое сравнение строк давало второй инвайт тому, кто попал в аудиторию из
    двух парсингов с разным регистром;
  * чтение журнала fail-open: при сбое звать всех, потому что сорванный инвайт
    хуже недодедупленного. Обратный выбор (не звать никого) тоже был бы молчащей
    поломкой.

ПОЧЕМУ НА ЖИВОМ POSTGRES. Проверяется ровно то, чего заглушка пула не делает:
предикат `group_key = ANY($2)`, первичный ключ журнала
(owner_id, group_key, target), на котором стоит идемпотентность записи, и
изоляция по владельцу. Рецепт запуска — в docstring
tests/test_op_finish_paths_e2e_postgres.py.
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

OWNER = 991782
OTHER_OWNER = 991783
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

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    from services.op_worker import _INVITE_LOG_DDL

    def _wipe():
        _run(pool.execute(_INVITE_LOG_DDL))
        _run(pool.execute(
            "DELETE FROM invite_target_log WHERE owner_id = ANY($1::bigint[])",
            [OWNER, OTHER_OWNER]))
    _wipe()
    yield
    _wipe()


@pytest.fixture
def dedup():
    from services import invite_dedup
    return invite_dedup


# ── Главное: второй прогон не зовёт уже приглашённых ─────────────────────────

def test_the_second_run_skips_everyone_already_invited(pool, dedup):
    keys = dedup.dedup_keys(group_ref="@nash_chat", channel_id=-1001234567890123)
    assert keys, "подготовка: ключи канала не собрались"

    first = ["@ivan", "@petr", "@olga"]
    assert _run(dedup.remember(pool, OWNER, keys, first, op_id=1)) is True

    # Деплой: исполнитель поднимается заново и получает ВЕСЬ список.
    whole = first + ["@sveta", "@kolya"]
    to_call, dup, _opt = _run(dedup.filter_new(pool, OWNER, keys, whole))
    assert sorted(to_call) == ["@kolya", "@sveta"], (
        f"после перезапуска позвали бы повторно: {to_call}")
    assert dup == 3, f"пропущенные уже приглашённые посчитаны неверно: {dup}"


def test_any_door_sees_the_invite_made_through_another(pool, dedup):
    """Канал приходит @username, ссылкой и числовым id — человек один."""
    by_username = dedup.dedup_keys(group_ref="@nash_chat")
    by_id = dedup.dedup_keys(channel_id=-1001234567890123)
    _run(dedup.remember(pool, OWNER, by_username, ["@ivan"], op_id=1))

    seen = _run(dedup.invited_keys(pool, OWNER, by_username + by_id))
    assert seen, "запись под одной формой канала не читается ни под какой"

    to_call, dup, _ = _run(dedup.filter_new(pool, OWNER, by_username, ["@ivan"]))
    assert to_call == [] and dup == 1, (
        "дверь не видит приглашения, сделанного через другую форму того же канала")


def test_the_same_person_in_another_case_is_the_same_person(pool, dedup):
    """'@Ivan' и '@ivan' — один человек; Telegram матчит username именно так."""
    keys = dedup.dedup_keys(group_ref="@nash_chat")
    _run(dedup.remember(pool, OWNER, keys, ["@Ivan"], op_id=1))

    to_call, dup, _ = _run(dedup.filter_new(pool, OWNER, keys, ["@ivan"]))
    assert to_call == [] and dup == 1, (
        "человек, попавший в аудиторию с другим регистром, получил бы второй инвайт")


def test_duplicates_inside_one_list_are_called_once(pool, dedup):
    keys = dedup.dedup_keys(group_ref="@nash_chat")
    to_call, _dup, _ = _run(dedup.filter_new(
        pool, OWNER, keys, ["@ivan", "@IVAN", "@ivan"]))
    assert to_call == ["@ivan"], f"один человек в списке трижды: {to_call}"


def test_remembering_the_same_target_twice_is_not_an_error(pool, dedup):
    """Пачка переотправляется при неподтверждённой записи — запись идемпотентна."""
    keys = dedup.dedup_keys(group_ref="@nash_chat")
    assert _run(dedup.remember(pool, OWNER, keys, ["@ivan"], op_id=1)) is True
    assert _run(dedup.remember(pool, OWNER, keys, ["@ivan"], op_id=2)) is True


# ── Чего журнал делать не должен ─────────────────────────────────────────────

def test_one_owner_does_not_hide_people_from_another(pool, dedup):
    keys = dedup.dedup_keys(group_ref="@nash_chat")
    _run(dedup.remember(pool, OWNER, keys, ["@ivan"], op_id=1))

    to_call, dup, _ = _run(dedup.filter_new(pool, OTHER_OWNER, keys, ["@ivan"]))
    assert to_call == ["@ivan"] and dup == 0, (
        "приглашения одного владельца прячут людей от другого — журнал "
        "не скоупится по owner_id")


def test_a_person_never_invited_is_not_skipped(pool, dedup):
    """Самопроверка: иначе тесты выше прошли бы на пустом журнале сами собой."""
    keys = dedup.dedup_keys(group_ref="@nash_chat")
    to_call, dup, _ = _run(dedup.filter_new(pool, OWNER, keys, ["@nikto"]))
    assert to_call == ["@nikto"] and dup == 0


def test_a_broken_journal_calls_everyone_rather_than_nobody(dedup):
    """Сорванный инвайт хуже недодедупленного — но выбор обязан быть осознанным."""
    class _Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("БД недоступна")

        async def fetch(self, *a, **k):
            raise RuntimeError("БД недоступна")

        async def fetchval(self, *a, **k):
            raise RuntimeError("БД недоступна")

        async def fetchrow(self, *a, **k):
            raise RuntimeError("БД недоступна")

    keys = dedup.dedup_keys(group_ref="@nash_chat")
    to_call, dup, _ = _run(dedup.filter_new(
        _Broken(), OWNER, keys, ["@ivan", "@petr"]))
    assert to_call == ["@ivan", "@petr"] and dup == 0, (
        "сбой чтения журнала остановил инвайт целиком")

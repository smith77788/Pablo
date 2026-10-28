"""Поиск в списке контактов находит по словам в любом порядке и по тегу.

ЧТО БЫЛО. Поле «🔍 Поиск контактов…» на экране контактов шлёт `search=` в
`/api/miniapp/uch/contacts`, а там условие было одно: `ILIKE '%вся фраза%'` по
семи колонкам. Два следствия, и оба выглядели для владельца одинаково — «такого
контакта нет»:

* «петров иван» не находило Ивана Петрова: такой подстроки нет ни в одной
  колонке. Спасал только `display_name`, и только если порядок слов совпал;
* тег не искался ВООБЩЕ. Владелец помечает контакты тегом `vip`, набирает «vip»
  в поиске и не находит никого — при этом поиск в БОТЕ по тегам ищет
  (`search_engine.search_contacts`). Та же база, тот же тег, разный ответ.

ЧТО ТЕПЕРЬ. Каждое слово обязано где-то совпасть (И между словами), и
совпадением считается точный тег. Условие собирается в `_segment_where`, то
есть остаётся ОБЩИМ для списка и для рабочего среза (рассылки/инвайты): «что
вижу в списке» и «на что применяю действие» по-прежнему считает один запрос —
инвариант, который этот файл проверяет отдельным тестом.

ПОЧЕМУ ЖИВАЯ БАЗА: условие собирается f-строкой с номерами плейсхолдеров, и
правду о них знает только Postgres (заглушка пула принимает любой SQL). Как
запускать — см. docstring tests/test_every_screen_survives_an_empty_account_postgres.py.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

os.environ.setdefault("DATABASE_URL", DSN or "postgresql://localhost/none")

OWNER = 776655443401
STRANGER = 776655443402

_LOOP: asyncio.AbstractEventLoop | None = None


def _run(coro):
    global _LOOP
    if _LOOP is None:
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    rows = [
        (OWNER, 2001, "ivanpetrov", "Иван", "Петров", "ООО Ромашка", ["vip"]),
        (OWNER, 2002, "mkuz", "Мария", "Кузнецова", "Ромашка", ["vip", "лид"]),
        (OWNER, 2003, "sidor", "Пётр", "Сидоров", "Acme", []),
        (STRANGER, 2004, "chuzhoy", "Иван", "Петров", "ООО Ромашка", ["vip"]),
    ]

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])",
                        [OWNER, STRANGER])
        for owner, tgid, un, fn, ln, co, tags in rows:
            await p.execute(
                "INSERT INTO unified_contacts(id, owner_id, telegram_user_id, username,"
                " first_name, last_name, company, tags, display_name)"
                " VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                uuid.uuid4(), owner, tgid, un, fn, ln, co, tags, f"{fn} {ln}")
        return p

    p = _run(_mk())
    yield p
    _run(p.execute("DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])",
                   [OWNER, STRANGER]))
    _run(p.close())
    global _LOOP
    if _LOOP is not None:
        _LOOP.close()
        _LOOP = None


def _list(pool, search):
    from services.contacts_hub.repository import get_contacts

    res = _run(get_contacts(pool, OWNER, search=search, limit=50))
    return {c["username"] for c in res["contacts"]}, res["total"]


def test_a_word_order_does_not_matter(pool):
    assert "ivanpetrov" in _list(pool, "иван петров")[0]
    assert "ivanpetrov" in _list(pool, "петров иван")[0], (
        "порядок слов решает — значит ищется фраза, а не слова")


def test_a_tag_is_searchable_from_the_list(pool):
    found, total = _list(pool, "vip")
    assert found == {"ivanpetrov", "mkuz"}, f"поиск по тегу нашёл {found}"
    assert total == 2, f"счётчик списка не совпал с выдачей: {total}"


def test_every_word_must_match(pool):
    """И между словами: лишнее слово сужает, а не расширяет выдачу."""
    assert _list(pool, "иван петров ромашка")[0] == {"ivanpetrov"}
    assert _list(pool, "иван кузнецова")[0] == set(), (
        "слова объединены через ИЛИ — выдача расширяется вместо сужения")


def test_a_short_word_is_still_a_filter(pool):
    """Короткое слово фильтрует, а не превращается в «показать всех».

    Буква, которой нет ни в одном контакте, обязана дать пустую выдачу: условие
    поиска, выпавшее из запроса (например, отброшенное как «слишком короткое»),
    выглядит именно как «нашлись все».
    """
    assert _list(pool, "щ")[0] == set(), "условие поиска выпало из запроса"
    assert _list(pool, "кузн")[0] == {"mkuz"}


def test_another_owners_contact_never_shows_up(pool):
    for q in ("иван петров", "vip", "ромашка"):
        assert "chuzhoy" not in _list(pool, q)[0], (
            f"в выдачу по «{q}» попал контакт другого владельца")


def test_the_list_and_the_working_slice_agree(pool):
    """Инвариант: «что вижу» и «на что применяю действие» — один запрос.

    Если условие поиска разъедется между списком и срезом, рассылка уйдёт не
    тем людям, которых владелец видел на экране. Сравниваем на запросе, где
    новое условие работает (слова + тег).
    """
    from services.contacts_hub.repository import count_segment, resolve_segment

    for q in ("иван петров", "vip", "ромашка"):
        seen, _total = _list(pool, q)
        filters = {"search": q, "include_excluded": True, "include_inactive": True}
        rows = _run(resolve_segment(pool, OWNER, filters, limit=50))
        acted = {r["username"] for r in rows}
        assert acted == seen, (
            f"по «{q}» список показывает {seen}, а действие применится к {acted}")
        assert _run(count_segment(pool, OWNER, filters)) == len(seen)

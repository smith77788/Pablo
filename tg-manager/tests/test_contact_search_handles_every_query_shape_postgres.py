"""Поиск по контактам работает на запросе из двух слов и с оператором.

ЧТО БЫЛО. `search_contacts` — главный поиск по контактам: его зовёт мини-апп
(`/api/miniapp/uch/search`) и бот (`bot/handlers/contacts_hub.py`). Он собирал
SQL четырьмя почти одинаковыми ветками и в трёх из них путал номера
плейсхолдеров:

* выражение ранжирования (`match_score`) строилось от индекса ПОСЛЕ слов
  запроса, то есть ссылалось на плейсхолдеры `limit`/`offset` — сравнивалось
  `first_name ILIKE <число>`;
* `LIMIT ${idx - 1} OFFSET ${idx}` брали на один меньше реальных позиций, и в
  `LIMIT` уезжала строка `%слово%`.

Postgres отвечал `argument of LIMIT must be type bigint, not type text`, и поиск
падал пятисоткой на ЛЮБОМ запросе из двух слов и на ЛЮБОМ запросе с оператором
(`@username`, `#тег`, `company:`, `phone:`). Работал ровно один случай — запрос
из одного слова, единственная ветка, где арифметика случайно совпала. Проверено
живой базой: 6 из 9 форм запроса падали.

Почему не поймали юнит-тесты: заглушка пула принимает любую строку SQL и молча
отдаёт заготовленный ответ — номера плейсхолдеров и типы параметров знает только
Postgres. Поэтому файл требует INFRAGRAM_TEST_DSN (как запускать — см. docstring
tests/test_every_screen_survives_an_empty_account_postgres.py).

ЧТО ТЕПЕРЬ. Одна сборка запроса вместо четырёх веток; ранжирование
переиспользует плейсхолдеры слов, а номера страницы считаются от фактической
длины списка параметров — единственной правды о том, что уедет в запрос.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

os.environ.setdefault("DATABASE_URL", DSN or "postgresql://localhost/none")

OWNER = 776655443399
STRANGER = 776655443400

# ОДИН цикл на весь файл: пул asyncpg привязан к циклу, в котором создан.
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
        (OWNER, 1001, "ivanpetrov", "Иван", "Петров", "ООО Ромашка", ["vip"]),
        (OWNER, 1002, "mkuz", "Мария", "Кузнецова", "Ромашка", ["vip"]),
        (OWNER, 1003, "ipetrovsky", "Иван", "Петровский", "Acme", []),
        (STRANGER, 1004, "ivanchuzhoy", "Иван", "Петров", "ООО Ромашка", ["vip"]),
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


def _search(pool, q, **kw):
    from services.contacts_hub.search_engine import _cache, search_contacts

    _cache.clear()
    return _run(search_contacts(pool, OWNER, q, **kw))


def _names(found) -> set[str]:
    return {r["username"] for r in found}


# Все формы запроса, которые умеет разобрать движок. Любая форма, падающая
# ошибкой БД, — это пятисотка на экране поиска.
SHAPES = [
    ("пустой запрос", "", {"ivanpetrov", "mkuz", "ipetrovsky"}),
    ("одно слово", "петров", {"ivanpetrov", "ipetrovsky"}),
    ("два слова", "иван петров", {"ivanpetrov", "ipetrovsky"}),
    ("три слова", "иван петров ромашка", {"ivanpetrov"}),
    ("оператор @username", "@ivanpetrov", {"ivanpetrov"}),
    ("оператор + слово", "@ivanpetrov петров", {"ivanpetrov"}),
    ("оператор + два слова", "@ivanpetrov иван петров", {"ivanpetrov"}),
    ("оператор #тег", "#vip", {"ivanpetrov", "mkuz"}),
    ("тег + слово", "#vip иван", {"ivanpetrov"}),
    ("оператор company:", "company:Ромашка", {"ivanpetrov", "mkuz"}),
]


@pytest.mark.parametrize("name,query,expected", SHAPES, ids=[s[0] for s in SHAPES])
def test_every_query_shape_returns_its_contacts(pool, name, query, expected):
    """Форма запроса не должна ронять базу — и должна находить именно своих."""
    assert _names(_search(pool, query)) == expected, f"форма «{name}» вернула не то"


def test_another_owners_contact_never_shows_up(pool):
    """У чужого контакта те же имя, фамилия, компания и тег — различает owner_id."""
    for _name, query, _exp in SHAPES:
        assert "ivanchuzhoy" not in _names(_search(pool, query)), (
            f"в выдачу по «{query}» попал контакт другого владельца")


def test_the_page_limit_is_honoured_on_a_multiword_query(pool):
    """`limit` раньше уезжал в плейсхолдер ранжирования, а не в LIMIT."""
    assert len(_search(pool, "иван петров", limit=1)) == 1


def test_rubbish_in_the_page_does_not_break_the_search(pool):
    """Мусор и отрицательные числа в странице — не пятисотка, а разумный предел."""
    assert _names(_search(pool, "иван петров", limit="abc")) == {
        "ivanpetrov", "ipetrovsky"}
    assert _names(_search(pool, "петров", offset="-5")) == {
        "ivanpetrov", "ipetrovsky"}
    assert len(_search(pool, "петров", limit=-1)) == 1, "LIMIT -1 — ошибка запроса"
    assert _search(pool, "петров", limit=10**12) != [], "огромный потолок зажат"


def test_the_whole_table_is_not_read_for_one_keystroke(pool):
    """Потолок страницы ограничен сверху: иначе клиент может попросить всё."""
    from services.contacts_hub import search_engine

    lim, off = search_engine._page(10**9, 10**12)
    assert lim == search_engine._PAGINATION_MAX_LIMIT
    assert off <= 10_000_000


def test_the_advanced_search_shares_the_same_arithmetic(pool):
    """`search_contacts_advanced` повторяла ту же сборку запроса своими тремя
    ветвями и была сломана так же. Теперь обе функции зовут один сборщик, и
    поломка в одном месте видна обоим тестам."""
    from services.contacts_hub.search_engine import _cache, search_contacts_advanced

    _cache.clear()
    res = _run(search_contacts_advanced(pool, OWNER, "иван петров", limit=10))
    assert {r["username"] for r in res["items"]} >= {"ivanpetrov", "ipetrovsky"}
    assert res["total"] == 2, f"счётчик найденного врёт: {res['total']}"
    assert res["has_more"] is False

    _cache.clear()
    page = _run(search_contacts_advanced(pool, OWNER, "иван петров", limit=1))
    assert len(page["items"]) == 1
    assert page["total"] == 2, "страница урезана, а счётчик найденного должен быть полным"
    assert page["has_more"] is True, "есть вторая страница, а интерфейсу об этом не сказали"


def test_one_builder_holds_the_placeholder_arithmetic():
    """Антивакуумность: пока сборка одна, оба теста выше стерегут один код.
    Вернётся копия арифметики — вернётся и класс ошибки."""
    import inspect

    from services.contacts_hub import search_engine

    src = inspect.getsource(search_engine)
    body = src[src.index("async def search_contacts("):]
    assert "${idx - 1}" not in body and "${idx + 1}" not in body, (
        "номера плейсхолдеров снова считаются арифметикой от idx")
    assert body.count("_build_search_query(") >= 2, (
        "сборка запроса снова разъехалась по функциям")

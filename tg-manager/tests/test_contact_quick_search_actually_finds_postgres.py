"""Быстрый поиск по контактам («спотлайт») находит контакт. Раньше — никогда.

ЧТО БЫЛО. `search_contacts_spotlight` собирала SQL f-строкой, и в двух местах
вместо placeholder'а `${idx}` стояло `$${idx}`. В Postgres `$$` открывает
dollar-quoted строковый литерал, поэтому запрос не разбирался ВООБЩЕ: любой
ввод давал `PostgresSyntaxError: syntax error at or near "2"` (а с оператором —
`unterminated dollar-quoted string`). Ручка `/api/miniapp/uch/spotlight`
отвечала пятисоткой на что угодно — то есть глобальный поиск по контактам не
работал ни разу с момента написания.

Почему это не поймали тесты: заглушка пула принимает ЛЮБУЮ строку SQL и молча
отдаёт заготовленный ответ. Синтаксис знает только Postgres — поэтому файл
требует INFRAGRAM_TEST_DSN (как запускать — см. docstring
tests/test_every_screen_survives_an_empty_account_postgres.py).

Нашлось пробой по всем GET-ручкам живой базой: 284 ручки × мусор в параметрах.

Рядом в той же функции, вскрыто тем же разбором:

* ранжирование (CASE) ссылалось на `${idx}`, который к тому моменту указывал
  уже не на шаблон поиска, а на `limit`: сравнивалось `first_name ILIKE <число>`;
* `LIMIT` брал захардкоженные 999999 вместо `limit`, а сам `limit` уезжал в
  запрос неиспользованным параметром. То есть на каждое нажатие в поиске
  читалась ВСЯ таблица контактов владельца; на сотне тысяч контактов это
  таймаут веб-процесса, а не медленный поиск;
* совпадение по тегу сравнивало тег с `%запрос%` — с процентами, поэтому точный
  тег не совпадал никогда.
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

OWNER = 776655443322          # свой владелец: чужие контакты в выдачу не лезут
STRANGER = 776655443323


# ОДИН цикл на весь файл. Пул asyncpg привязывается к циклу, в котором создан:
# цикл-на-вызов (new_event_loop/close вокруг каждой корутины) убивает пул сразу
# после фикстуры, и дальше всё падает «Event loop is closed».
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

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        await p.execute("DELETE FROM unified_contacts WHERE owner_id = ANY($1::bigint[])",
                        [OWNER, STRANGER])
        rows = [
            (OWNER, 1001, "ivanpetrov", "Иван", "Петров", "ООО Ромашка", ["vip", "лид"]),
            (OWNER, 1002, "obrien", "O'Brien", "Smith", "Acme", []),
            (OWNER, 1003, "mkuznecov", "Мария", "Кузнецова", "Ромашка", ["vip"]),
            (STRANGER, 1004, "ivanchuzhoy", "Иван", "Чужой", "Не наше", ["vip"]),
        ]
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
    from services.contacts_hub.search_engine import search_contacts_spotlight, _cache

    _cache._store.clear() if hasattr(_cache, "_store") else None
    return _run(search_contacts_spotlight(pool, OWNER, q, **kw))


def test_a_plain_query_finds_the_contact(pool):
    found = _search(pool, "петров")
    assert found, "обычный текстовый поиск не нашёл ничего — запрос вообще не разобрался"
    assert any(r["username"] == "ivanpetrov" for r in found)


def test_an_apostrophe_is_just_a_letter(pool):
    """Кавычка в фамилии — обычный символ, а не поломка запроса."""
    found = _search(pool, "O'Brien")
    assert any(r["username"] == "obrien" for r in found), "контакт с кавычкой не найден"


def test_an_injection_attempt_is_only_a_search_string(pool):
    found = _search(pool, "'; DROP TABLE unified_contacts; --")
    assert found == [] or isinstance(found, list)
    left = _run(pool.fetchval("SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1",
                              OWNER))
    assert left == 3, "таблица пострадала от строки поиска"


def test_wildcards_from_the_user_do_not_match_everything(pool):
    """`%` и `_` в ILIKE — метасимволы; экран ищет текст, а не шаблон."""
    assert _search(pool, "%") == [], "процент в поиске вернул всех подряд"
    assert _search(pool, "_") == [], "подчёркивание в поиске вернуло всех подряд"


def test_an_operator_query_works_too(pool):
    found = _search(pool, "@ivanpetrov")
    assert any(r["username"] == "ivanpetrov" for r in found), (
        "поиск по оператору @username не разобрался")


def test_a_tag_matches_exactly_not_by_pattern(pool):
    found = _search(pool, "vip")
    assert len(found) >= 2, "поиск по тегу не нашёл помеченных контактов"
    assert {r["username"] for r in found} >= {"ivanpetrov", "mkuznecov"}


def test_another_owners_contact_never_shows_up(pool):
    for q in ("иван", "vip", "чужой"):
        assert all(r["owner_id"] == OWNER for r in _search(pool, q)), (
            f"в выдачу по «{q}» попал контакт другого владельца")


def test_the_limit_is_respected_and_the_whole_table_is_not_read(pool):
    """`limit` раньше уезжал в запрос неиспользованным: LIMIT брал 999999."""
    found = _search(pool, "ромашка", limit=1)
    assert len(found) == 1, f"limit=1 вернул {len(found)}"
    seen = {}

    class _Spy:
        async def fetch(self, q, *a):
            seen["sql"], seen["args"] = q, a
            return await pool.fetch(q, *a)

        async def fetchval(self, q, *a):
            return await pool.fetchval(q, *a)

    _search(_Spy(), "ромашка", limit=7)
    assert "999999" not in seen["sql"], "в запросе снова захардкожен потолок"
    assert len(seen["args"]) == sum(
        1 for i in range(1, len(seen["args"]) + 1) if f"${i}" in seen["sql"]), (
        "в запрос уезжают параметры, которых в нём нет "
        f"(args={len(seen['args'])}, sql={seen['sql'][:120]})")

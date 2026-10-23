"""Доверие давало бонус за телефон КАЖДОМУ контакту.

НАХОДКА (23.10.2026). `compute_trust_score` проверяла телефон так:
`if contact_data.get('phones') and len(contact_data['phones']) > 0`. Колонка
`unified_contacts.phones` — JSONB (schema_v138), asyncpg отдаёт jsonb СТРОКОЙ
(кодек json на пуле нигде не ставится), и у пустого значения строка выглядит
как `'[]'` — непустая, длиной 2. То есть условие было правдой ВСЕГДА, и +0.1
получал каждый контакт: признак «есть телефон» в оценке доверия не значил
ничего, а число в карточке было завышено у всех, у кого телефона нет.

Путь живой: `update_trust_scores` передаёт `dict(c)` прямо из `SELECT *` и
записывает результат в `trust_score`, который видно в карточке и по которому
сортируется список.

Это тот же класс, что ложный «один телефон» в поиске дублей, в том же модуле:
колонка jsonb читается через `_as_list`, никогда напрямую.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): вернул `len(contact_data['phones']) > 0`
-> красные test_an_empty_jsonb_phone_list_is_not_a_phone,
test_a_parsed_list_works_the_same_way и
test_the_live_recount_separates_a_phone_from_an_empty_one_postgres.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services.contacts_hub.trust_engine import compute_trust_score

DSN = os.environ.get("INFRAGRAM_TEST_DSN")
OWNER = 991901


def test_an_empty_jsonb_phone_list_is_not_a_phone():
    """`'[]'` — это «телефона нет», а не строка длиной 2."""
    empty = compute_trust_score({"telegram_user_id": 1, "phones": "[]"}, [])
    real = compute_trust_score(
        {"telegram_user_id": 1, "phones": '["+79990000001"]'}, [])
    assert real > empty, (
        f"контакт без телефона получает столько же, сколько с телефоном "
        f"({empty} и {real}): признак в оценке не значит ничего")
    assert round(real - empty, 3) == 0.1, (real, empty)


def test_a_parsed_list_works_the_same_way():
    """Та же функция зовётся и с уже разобранным списком — ответ один."""
    s_str = compute_trust_score({"phones": '["+79990000001"]'}, [])
    s_list = compute_trust_score({"phones": ["+79990000001"]}, [])
    assert s_str == s_list, (s_str, s_list)
    assert compute_trust_score({"phones": []}, []) == compute_trust_score(
        {"phones": "[]"}, [])


def test_junk_in_the_column_is_not_a_phone():
    """Нечитаемое значение — это «неизвестно», а не «телефон есть»."""
    assert compute_trust_score({"phones": "не json"}, []) == compute_trust_score(
        {"phones": "[]"}, [])


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
def test_the_live_recount_separates_a_phone_from_an_empty_one_postgres():
    """Пересчёт по всему хабу обязан различать эти два контакта."""
    import asyncpg

    from services.contacts_hub.trust_engine import update_trust_scores

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            with_phone = await pool.fetchval(
                "INSERT INTO unified_contacts(owner_id, telegram_user_id, first_name, phones) "
                "VALUES($1,$2,'С телефоном','[\"+79990000001\"]'::jsonb) RETURNING id",
                OWNER, 9001)
            without = await pool.fetchval(
                "INSERT INTO unified_contacts(owner_id, telegram_user_id, first_name, phones) "
                "VALUES($1,$2,'Без телефона','[]'::jsonb) RETURNING id",
                OWNER, 9002)
            n = await update_trust_scores(pool, OWNER)
            a = await pool.fetchval(
                "SELECT trust_score FROM unified_contacts WHERE id=$1", with_phone)
            b = await pool.fetchval(
                "SELECT trust_score FROM unified_contacts WHERE id=$1", without)
            return n, float(a), float(b)
        finally:
            await pool.close()

    n, a, b = asyncio.new_event_loop().run_until_complete(go())
    assert n == 2, f"пересчёт тронул {n} контактов вместо двух"
    assert a > b, (
        f"после пересчёта доверие одинаковое ({a} и {b}): бонус за телефон "
        "снова достаётся тому, у кого телефона нет")

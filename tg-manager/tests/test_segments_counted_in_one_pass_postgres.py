"""Список сохранённых сегментов: все размеры одним проходом — и те же числа.

`list_segments` считал размер КАЖДОГО сегмента отдельным проходом по контактам
владельца. На стенде (30 000 контактов, 2000 сегментов) экран
`/api/miniapp/uch/segments` шёл 13,7 с и падал в таймаут; теперь 0,45 с.
`count_segments` склеивает условия того же `_segment_where` в один SELECT с
`COUNT(*) FILTER`, перенумеровав параметры. Ошибка перенумерации дала бы
правдоподобные, но чужие числа — поэтому сверяем с поштучным счётом на
живой базе по всем видам фильтра сразу.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres")

OWNER = 995321


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


FILTERS = [
    {},
    {"search": "анна"},
    {"search": "петров иван"},
    {"tag": "vip"},
    {"favorite_only": True},
    {"with_phone_only": True, "tag": "vip"},
    {"with_username_only": True, "search": "ivan"},
    {"premium_only": True},
    {"gender": "f"},
    {"include_excluded": True},
    {"excluded_only": True},
    {"include_inactive": True, "search": "петров"},
]


def test_one_pass_counts_equal_one_by_one():
    import asyncpg

    from services.contacts_hub import repository as repo

    async def _go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            people = [
                ("Анна", "Петрова", "anna", ["vip"], True, False, '["+7999"]'),
                ("Иван", "Петров", "ivan", ["vip", "b2b"], False, False, "[]"),
                ("Иван", "Сидоров", None, [], False, True, '["+7998"]'),
                ("Мария", None, "masha", ["vip"], True, True, "[]"),
                ("Пётр", "Иванов", "petr", [], False, False, '["+7997"]'),
            ]
            for i, (fn, ln, un, tags, fav, excl, phones) in enumerate(people):
                await pool.execute(
                    "INSERT INTO unified_contacts(owner_id, first_name, last_name, username, "
                    "display_name, tags, is_favorite, excluded, phones, gender, is_premium) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11)",
                    OWNER, fn, ln, un, f"{fn} {ln or ''}".strip(), tags, fav, excl, phones,
                    "f" if fn in ("Анна", "Мария") else "m", i % 2 == 0)
            class _Counting:
                """Пул, считающий запросы: поштучный запасной путь — тоже провал."""
                calls = 0

                async def fetchrow(self, *a):
                    _Counting.calls += 1
                    return await pool.fetchrow(*a)

                async def fetchval(self, *a):
                    _Counting.calls += 1
                    return await pool.fetchval(*a)

            batch = await repo.count_segments(_Counting(), OWNER, FILTERS)
            assert _Counting.calls == 1, (
                f"{_Counting.calls} запросов: общий счёт не прошёл и ушёл в поштучный")
            single = [await repo.count_segment(pool, OWNER, f) for f in FILTERS]
            return batch, single
        finally:
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWNER)
            await pool.close()

    batch, single = _run(_go())
    assert None not in batch, f"общий счёт упал и ушёл в поштучный: {batch}"
    assert batch == single, list(zip(FILTERS, batch, single))
    assert len(set(single)) > 2, f"фильтры ничего не различают — тест ничего не проверяет: {single}"

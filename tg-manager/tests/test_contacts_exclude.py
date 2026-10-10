"""Контакты: личные исключаются из работы; массовые операции по фильтру.

Живой Postgres: суть — рабочий срез (count_segment/resolve_segment) НЕ видит
помеченных личными, а обычный список — видит; удаление/пометка по стране (из
префикса номера) и тегу.
"""
from __future__ import annotations

import json
import os

import pytest

from services.contacts_hub import repository as repo
from services.contacts_hub import bulk_ops_engine as bulk
from services.account_manager import country_code_from_phone

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_contacts_exclude_and_filter_postgres():
    import asyncio
    import asyncpg

    OWN = 805000
    RU_PHONE = "+79991234567"
    US_PHONE = "+12025550123"
    ru_iso = country_code_from_phone(RU_PHONE)
    us_iso = country_code_from_phone(US_PHONE)

    async def go():
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)

        async def _cl():
            await pool.execute("DELETE FROM unified_contacts WHERE owner_id=$1", OWN)

        async def _mk(name, phone, tags):
            return str(await pool.fetchval(
                "INSERT INTO unified_contacts(owner_id, first_name, phones, tags) "
                "VALUES($1,$2,$3::jsonb,$4) RETURNING id",
                OWN, name, json.dumps([phone] if phone else []), tags))

        try:
            await _cl()
            c_ru = await _mk("Мама", RU_PHONE, ["family"])
            c_us = await _mk("Партнёр", US_PHONE, ["work"])
            c_ru2 = await _mk("Клиент", RU_PHONE, ["work"])

            # исходно все 3 — в рабочем срезе и в списке
            assert await repo.count_segment(pool, OWN, {}) == 3
            assert (await repo.get_contacts(pool, OWN))["total"] == 3

            # пометить «Маму» личной → уходит из рабочего среза, остаётся в списке
            r = await bulk.set_excluded(pool, OWN, [c_ru], True)
            assert r["updated"] == 1
            assert await repo.count_segment(pool, OWN, {}) == 2       # рабочий срез
            assert (await repo.get_contacts(pool, OWN))["total"] == 3  # список видит всех
            # режим «только личные»
            only = await repo.get_contacts(pool, OWN, excluded_only=True)
            assert only["total"] == 1 and only["contacts"][0]["first_name"] == "Мама"

            # снять пометку — вернулась в работу
            await bulk.set_excluded(pool, OWN, [c_ru], False)
            assert await repo.count_segment(pool, OWN, {}) == 3

            # фильтр по стране: RU-номера — двое
            ids_ru = await bulk.resolve_filter_ids(pool, OWN, country=ru_iso)
            assert set(ids_ru) == {c_ru, c_ru2}
            assert await bulk.count_by_filter(pool, OWN, country=us_iso) == 1

            # массовая пометка личными по стране (RU) → рабочий срез = только US
            await bulk.exclude_by_filter(pool, OWN, excluded=True, country=ru_iso)
            assert await repo.count_segment(pool, OWN, {}) == 1
            await bulk.exclude_by_filter(pool, OWN, excluded=False, country=ru_iso)

            # удаление по фильтру: тег work → двое (US + RU2), Мама (family) остаётся
            res = await bulk.delete_by_filter(pool, OWN, tag="work")
            assert res["deleted"] == 2
            left = await repo.get_contacts(pool, OWN)
            assert left["total"] == 1 and left["contacts"][0]["first_name"] == "Мама"

            # пустой фильтр ничего не удаляет (защита)
            assert (await bulk.delete_by_filter(pool, OWN))["deleted"] == 0
            await _cl()
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())

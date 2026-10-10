"""Provider Marketplace (блок 1: провайдеры + каталог).

Чистые тесты (деньги/ключи/валидация) идут в CI. Полный жизненный цикл против
живой схемы — на Postgres (INFRAGRAM_TEST_DSN): заглушка пула не проверяет типы
связывания, а тут важны numeric/jsonb/partial-unique.
"""
from __future__ import annotations

import os

import pytest

from services import provider_marketplace as mp


# ── Чистые: деньги ────────────────────────────────────────────────────────────
def test_to_cents_and_format():
    assert mp.to_cents("12.50") == 1250
    assert mp.to_cents("0") == 0
    assert mp.to_cents(3) == 300
    assert mp.to_cents("1,99") == 199          # запятая как разделитель
    assert mp.format_cents(1250, "USD") == "12.50 USD"
    assert mp.format_cents(5, "EUR") == "0.05 EUR"


def test_to_cents_rejects_bad():
    for bad in ("-5", "abc", "", "nan", "inf"):
        with pytest.raises(ValueError):
            mp.to_cents(bad)


def test_commission_split_integer_and_bps():
    # 10.00 цена, комиссия 15% (1500 bps), маржа резеллера 2.00
    r = mp.commission_split(1000, 1500, 200)
    assert r == {"provider_cents": 1000, "platform_cents": 150,
                 "reseller_cents": 200, "final_cents": 1350}
    # нулевая комиссия
    assert mp.commission_split(999, 0)["final_cents"] == 999
    # округление комиссии ВНИЗ (площадка не завышает): 333 * 3.33% = 11.0889 → 11
    assert mp.commission_split(333, 333)["platform_cents"] == 11


# ── Чистые: ключи и slug ──────────────────────────────────────────────────────
def test_api_key_roundtrip():
    full, prefix, h = mp.gen_api_key()
    assert full.startswith("mpk_") and full.startswith(prefix)
    assert mp.hash_api_key(full) == h and len(h) == 64
    # разные вызовы — разные ключи
    assert mp.gen_api_key()[0] != mp.gen_api_key()[0]


def test_slugify():
    assert mp.slugify("Acme Proxies, LLC!") == "acme-proxies-llc"
    assert mp.slugify("   ") == "provider"


def test_validate_service_fields():
    mp.validate_service_fields(0, 1, 10)         # ok
    with pytest.raises(ValueError):
        mp.validate_service_fields(-1, 1, 10)
    with pytest.raises(ValueError):
        mp.validate_service_fields(10, 0, 10)
    with pytest.raises(ValueError):
        mp.validate_service_fields(10, 5, 3)


# ── Живой Postgres: полный жизненный цикл ─────────────────────────────────────
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")
def test_provider_catalog_lifecycle_postgres():
    import asyncio
    import glob
    import re
    import asyncpg

    async def go():
        # применяем схему (idempotent) на отдельной БД теста
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass
        await conn.close()
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM mp_providers WHERE owner_id=900777")
            p = await mp.register_provider(pool, 900777, "QA Proxies", category="proxy")
            pid = p["id"]
            assert p["status"] == "pending"
            s = await mp.create_service(pool, pid, title="RU proxy", category="proxy",
                                        resource_kind="proxy",
                                        price_cents=mp.to_cents("2.99"), stock=10)
            # pending → не в публичном каталоге
            assert not any(r["provider_id"] == pid
                           for r in await mp.browse_catalog(pool, category="proxy"))
            await mp.set_provider_status(pool, pid, "verified", by=1)
            cat = [r for r in await mp.browse_catalog(pool, category="proxy")
                   if r["provider_id"] == pid]
            assert cat and cat[0]["provider_name"] == "QA Proxies"
            # api key
            ik = await mp.issue_api_key(pool, pid)
            assert (await mp.authenticate_api_key(pool, ik["api_key"]))["id"] == pid
            assert await mp.authenticate_api_key(pool, "mpk_bad") is None
            # sync идемпотентен
            r1 = await mp.sync_services(pool, pid, [
                {"external_id": "e1", "title": "A", "price_cents": 100}])
            r2 = await mp.sync_services(pool, pid, [
                {"external_id": "e1", "title": "A2", "price_cents": 120}])
            assert r1["created"] == 1 and r2["updated"] == 1
            # suspend блокирует auth и каталог
            await mp.set_provider_status(pool, pid, "suspended", by=1)
            assert await mp.authenticate_api_key(pool, ik["api_key"]) is None
            assert not any(r["provider_id"] == pid
                           for r in await mp.browse_catalog(pool, category="proxy"))
            # revoke
            await mp.set_provider_status(pool, pid, "verified", by=1)
            assert await mp.revoke_api_key(pool, pid, ik["id"])
            assert await mp.authenticate_api_key(pool, ik["api_key"]) is None
            await pool.execute("DELETE FROM mp_providers WHERE owner_id=900777")
        finally:
            await pool.close()

    asyncio.new_event_loop().run_until_complete(go())

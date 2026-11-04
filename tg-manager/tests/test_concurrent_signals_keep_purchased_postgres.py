"""Два сигнала об одном человеке разом не стирают «Купил» — живая база.

ЧТО БЫЛО. `virtual_layer.signal` читал состояние, решал и писал БЕЗУСЛОВНО.
«Заплатил» из платёжки и «ответил» из бота, пришедшие одновременно: второй
прочитал «Интерес», пока первый писал «Купил», и записал «Интерес» поверх.
Терминальное состояние стиралось, и купившего снова дожимали рассылками.
"""
from __future__ import annotations

import asyncio
import os

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен INFRAGRAM_TEST_DSN (живой Postgres)")

OWNER = 330978
EID = "c-race-1"


def test_paid_landing_between_read_and_write_survives(monkeypatch):
    import asyncpg
    from services import virtual_layer as vl

    async def go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            for t in ("virtual_state_history", "virtual_states"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWNER)
            await vl.signal(pool, OWNER, vl.USER, EID, "replied")
            assert (await vl.get_state(pool, OWNER, vl.USER, EID))["value"] == "interested"

            real_get = vl.get_state
            fired = {"done": False}

            async def _get_then_pay(*a, **k):
                state = await real_get(*a, **k)
                if not fired["done"]:
                    fired["done"] = True
                    # Пока «ответил» решает, приходит оплата.
                    monkeypatch.setattr(vl, "get_state", real_get)
                    await vl.signal(pool, OWNER, vl.USER, EID, "paid")
                    monkeypatch.setattr(vl, "get_state", _get_then_pay)
                return state

            monkeypatch.setattr(vl, "get_state", _get_then_pay)
            await vl.signal(pool, OWNER, vl.USER, EID, "replied")
            monkeypatch.setattr(vl, "get_state", real_get)
            st = await vl.get_state(pool, OWNER, vl.USER, EID)
            assert st["value"] == "purchased", (
                f"одновременный сигнал стёр «Купил»: {st['value']}")
        finally:
            for t in ("virtual_state_history", "virtual_states"):
                await pool.execute(f"DELETE FROM {t} WHERE owner_id=$1", OWNER)
            await pool.close()

    asyncio.run(go())

"""Код Infragram на живой базе (schema_v381): постановка, подтверждение,
замок после промахов, подтверждение на СВОЙ сеанс, снятие, подсказка.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import owner_guard as og
from services import owner_sessions as osess

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 887123
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres (INFRAGRAM_TEST_DSN)")


def _meta(ua="Phone G"):
    return {"ua": ua, "fp": osess.device_fp(ua), "label": osess.device_label(ua),
            "ip": "198.51.100.9", "country": "UA"}


def test_set_confirm_lock_remove():
    async def _go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            await pool.execute("DELETE FROM owner_guard WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM owner_sessions WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)

            # Кода нет: опасное действие пускается, подсказки нет (нет аккаунтов).
            g = await og.guard_dangerous(pool, OWNER, None)
            assert g["allowed"] and not g["need_pin"]
            assert await og.pin_suggested(pool, OWNER) is False

            # Есть аккаунт, кода нет → подсказка.
            await pool.execute(
                "INSERT INTO tg_accounts(owner_id, phone, session_str) VALUES($1,'+100','s')",
                OWNER)
            assert await og.pin_suggested(pool, OWNER) is True

            # Ставим код, два сеанса.
            a = await osess.open_session(pool, OWNER, "telegram", _meta("Phone A"), notify=False)
            b = await osess.open_session(pool, OWNER, "telegram", _meta("Laptop B"), notify=False)
            assert (await og.set_pin(pool, OWNER, "4821"))["ok"]
            assert await og.pin_suggested(pool, OWNER) is False, "код есть — подсказки нет"

            # Код есть → опасное действие требует подтверждения; неподтверждённый
            # сеанс не пускается.
            g = await og.guard_dangerous(pool, OWNER, b)
            assert not g["allowed"] and g["need_pin"]

            # Неверный код копит промахи, подтверждения не даёт.
            for _ in range(2):
                assert not (await og.verify_and_confirm(pool, OWNER, b, "0000"))["ok"]
            assert (await og.verify_and_confirm(pool, OWNER, b, "4821"))["ok"]
            # Подтвердился ТОЛЬКО сеанс b, не a.
            assert (await og.guard_dangerous(pool, OWNER, b))["allowed"]
            assert not (await og.guard_dangerous(pool, OWNER, a))["allowed"], (
                "подтверждение одного сеанса не открывает другой")

            # Промахи до замка (счётчик уже сброшен удачным вводом).
            res = None
            for _ in range(og.MAX_FAILS):
                res = await og.verify_and_confirm(pool, OWNER, b, "9999")
            assert res["locked_until"] is not None, "после пяти промахов — замок"
            # В замке даже верный код не проходит.
            assert not (await og.verify_and_confirm(pool, OWNER, b, "4821"))["ok"]

            # Снять код нельзя чужим кодом; верным — можно. Сбрасываем счётчик
            # и замок (иначе первый же неверный ввод снова запер бы на 15 минут).
            await pool.execute("UPDATE owner_guard SET failed_attempts=0, locked_until=NULL "
                               "WHERE owner_id=$1", OWNER)
            assert not (await og.remove_pin(pool, OWNER, "0000"))["ok"]
            await pool.execute("UPDATE owner_guard SET failed_attempts=0, locked_until=NULL "
                               "WHERE owner_id=$1", OWNER)
            assert (await og.remove_pin(pool, OWNER, "4821"))["ok"]
            assert not (await og.get_state(pool, OWNER))["has_pin"]
            # Без кода — снова пускаем.
            assert (await og.guard_dangerous(pool, OWNER, b))["allowed"]
        finally:
            await pool.execute("DELETE FROM owner_guard WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM owner_sessions WHERE owner_id=$1", OWNER)
            await pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
            await pool.close()
    asyncio.new_event_loop().run_until_complete(_go())


def test_change_requires_current_pin():
    async def _go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        try:
            await pool.execute("DELETE FROM owner_guard WHERE owner_id=$1", OWNER)
            assert (await og.set_pin(pool, OWNER, "1111"))["ok"]
            assert not (await og.set_pin(pool, OWNER, "2222", current_pin="9999"))["ok"], (
                "смена без верного текущего кода")
            assert (await og.set_pin(pool, OWNER, "2222", current_pin="1111"))["ok"]
            row = await pool.fetchrow("SELECT pin_hash FROM owner_guard WHERE owner_id=$1", OWNER)
            assert og.verify_pin("2222", row["pin_hash"]) and not og.verify_pin("1111", row["pin_hash"])
        finally:
            await pool.execute("DELETE FROM owner_guard WHERE owner_id=$1", OWNER)
            await pool.close()
    asyncio.new_event_loop().run_until_complete(_go())

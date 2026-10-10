"""Аккаунтный слой WB Chat на НАСТОЯЩЕМ Postgres: вход, ротация, mass_dm, воркер.

Проверяет реальный SQL (ON CONFLICT, частичные индексы, бюджеты, FOR UPDATE SKIP
LOCKED) — заглушкой пула это невоспроизводимо. Транспорт — мок-драйвер, поэтому
сеть не нужна; проверяется весь конвейер поверх абстракции.

КАК ЗАПУСТИТЬ (Postgres 16):
    export INFRAGRAM_TEST_DSN="postgresql://postgres@/infra?host=/var/tmp/pgtest/sock&port=55432"
    pytest tests/test_wb_chat_accounts_postgres.py -v

Без INFRAGRAM_TEST_DSN файл пропускается — обычный прогон и CI не ломаются.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 770077


@pytest_asyncio.fixture
async def pool():
    import asyncpg
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    # Чистая схема WB на каждый прогон.
    async with p.acquire() as c:
        await c.execute(
            """DROP TABLE IF EXISTS wb_account_budget, wb_operation_targets,
                   wb_operations, wb_accounts CASCADE"""
        )
        with open(os.path.join(ROOT, "schema_v185_wb_chat.sql"), encoding="utf-8") as f:
            await c.execute(f.read())
    yield p
    await p.close()


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    # Мок-транспорт + нет реальных пауз в mass_dm (иначе тест ждал бы секунды).
    monkeypatch.setenv("WB_CHAT_DRIVER", "mock")
    from services.wb_chat.drivers.mock import reset_mock_state
    reset_mock_state()

    async def _no_sleep(*_a, **_k):
        return None

    from services.wb_chat.engines import mass_dm
    monkeypatch.setattr(mass_dm.asyncio, "sleep", _no_sleep)
    yield
    reset_mock_state()


async def _make_account(pool, phone: str) -> dict:
    """Создать активный аккаунт через реальный поток входа (мок-драйвер)."""
    from services.wb_chat import login
    token = await login.start(phone, owner_id=OWNER, driver="mock")
    return await login.complete(pool, token, "0000")


# ── Вход и хранение ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_login_creates_encrypted_account(pool):
    acc = await _make_account(pool, "+79990001111")
    assert acc["status"] == "active"
    assert acc["phone"] == "+79990001111"
    # Сессия в БД зашифрована (ENC:*), а на чтении расшифрована.
    raw = await pool.fetchval("SELECT session_enc FROM wb_accounts WHERE id=$1", acc["id"])
    assert raw.startswith("ENC:")
    assert acc["session"] and not acc["session"].startswith("ENC:")


@pytest.mark.asyncio
async def test_login_same_phone_upserts(pool):
    a1 = await _make_account(pool, "+79990002222")
    a2 = await _make_account(pool, "+79990002222")
    assert a1["id"] == a2["id"]  # апсерт, не дубликат
    n = await pool.fetchval("SELECT COUNT(*) FROM wb_accounts WHERE owner_id=$1", OWNER)
    assert n == 1


# ── Ротация и бюджеты ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pick_account_respects_budget(pool):
    from services.wb_chat import accounts
    acc = await _make_account(pool, "+79990003333")
    picked = await accounts.pick_account(pool, OWNER, action_type="dm", daily_budget=2)
    assert picked and picked["id"] == acc["id"]
    # Исчерпываем дневной бюджет → аккаунт больше не выбирается.
    await accounts.incr_budget(pool, acc["id"], "dm", 2)
    assert await accounts.pick_account(pool, OWNER, action_type="dm", daily_budget=2) is None


@pytest.mark.asyncio
async def test_pick_account_skips_cooldown(pool):
    from services.wb_chat import accounts
    acc = await _make_account(pool, "+79990004444")
    await accounts.penalize(pool, acc["id"], seconds=3600)  # улетел в кулдаун
    assert await accounts.pick_account(pool, OWNER, action_type="dm") is None


# ── Массовая рассылка end-to-end ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mass_dm_end_to_end(pool):
    from services.wb_chat import op_worker
    from services.wb_chat.drivers.mock import SENT_OUTBOX
    await _make_account(pool, "+79990005555")

    op_id = await op_worker.enqueue(
        pool, owner_id=OWNER, op_type="mass_dm",
        payload={"text": "рассылка"},
        targets=["@a", "@b", "invalid:c", "@d"],
    )
    op = await op_worker._claim_next(pool)
    assert op and op["id"] == op_id
    await op_worker.run_one(pool, op)

    row = await pool.fetchrow("SELECT status, result FROM wb_operations WHERE id=$1", op_id)
    assert row["status"] == "done"
    import json
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert result["sent"] == 3          # @a, @b, @d
    assert result["skipped"] == 1       # invalid:c
    # Реально «отправлено» через мок-транспорт.
    assert SENT_OUTBOX.get("a") == ["рассылка"]
    assert "c" not in SENT_OUTBOX
    # Бюджет аккаунта увеличился на число успешных.
    used = await pool.fetchval(
        "SELECT used FROM wb_account_budget WHERE action_type='dm'"
    )
    assert used == 3


@pytest.mark.asyncio
async def test_mass_dm_real_driver_fails_gracefully(pool):
    from services.wb_chat import op_worker
    await _make_account(pool, "+79990006666")
    op_id = await op_worker.enqueue(
        pool, owner_id=OWNER, op_type="mass_dm",
        payload={"text": "x"}, targets=["@a"],
    )
    op = await op_worker._claim_next(pool)
    # Реальный драйвер без протокола → операция падает в failed с внятной причиной.
    await op_worker.run_one(pool, op, driver="real")
    row = await pool.fetchrow("SELECT status, error FROM wb_operations WHERE id=$1", op_id)
    assert row["status"] == "failed"
    assert "протокол" in (row["error"] or "").lower()


@pytest.mark.asyncio
async def test_claim_next_is_exclusive(pool):
    from services.wb_chat import op_worker
    await op_worker.enqueue(pool, owner_id=OWNER, op_type="mass_dm", payload={"text": "x"}, targets=["@a"])
    first = await op_worker._claim_next(pool)
    second = await op_worker._claim_next(pool)
    assert first is not None
    assert second is None  # уже running → повторно не выдаётся

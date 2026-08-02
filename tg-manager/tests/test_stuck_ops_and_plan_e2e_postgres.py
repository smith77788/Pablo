"""Живой Postgres: вотчдог застрявших операций не бьёт ложной тревогой по
ОТЛОЖЕННЫМ операциям.

Ошибку видел пользователь (скриншот): «Застрявшие операции #112 mass_invite
pending 71 мин (owner …)» — это была инвайт-континуация «продолжим завтра»
(scheduled_for в будущем). Поллер её корректно ЖДАЛ, а вотчдог считал застрявшей
и спамил админам чужими owner_id. Фикс: вотчдог, как и поллер, пропускает
операции с scheduled_for в будущем.

(Резолв тарифа «Enterprise → Free» проверяется отдельно, без БД, в
test_dashboard_plan_resolve.py.)

Заглушка пула типы/семантику SQL не проверяет — нужен живой драйвер. Без
INFRAGRAM_TEST_DSN файл пропускается (см. docstring test_invite_e2e_postgres.py).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN (см. docstring)")

OWNER_A = 993001   # «наш» владелец
OWNER_B = 993002   # чужой владелец — его отложенная op не должна утечь админу

_LOOP = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
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
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


class _FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((int(chat_id), text))


# ── вотчдог не считает отложенные операции застрявшими ───────────────────────

def test_watchdog_ignores_scheduled_ops(pool, monkeypatch):
    from services import op_worker

    async def _seed():
        await pool.execute("DELETE FROM operation_queue WHERE owner_id=ANY($1::bigint[])",
                           [OWNER_A, OWNER_B])
        # (1) отложенная континуация ЧУЖОГО владельца: pending, создана давно, но
        #     scheduled_for в будущем → НЕ застряла (поллер её ждёт).
        sched = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,created_at,scheduled_for) "
            "VALUES($1,'mass_invite','pending',$2, now()-interval '120 min', now()+interval '10 hours') "
            "RETURNING id", OWNER_B, json.dumps({}))
        # (2) реально застрявшая: pending, просрочена, scheduled_for уже наступил.
        stuck = await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,created_at,scheduled_for) "
            "VALUES($1,'mass_invite','pending',$2, now()-interval '120 min', now()-interval '30 min') "
            "RETURNING id", OWNER_A, json.dumps({}))
        return sched, stuck

    sched_id, stuck_id = _run(_seed())

    # Админ есть, дедуп сброшен, бот — фейковый (ловим текст алерта).
    monkeypatch.setenv("ADMIN_IDS", "424242")
    op_worker._alerted_stuck_ops.clear()
    bot = _FakeBot()
    _run(op_worker._watchdog_alerts(pool, bot))

    text = "\n".join(t for _, t in bot.sent)
    assert f"#{stuck_id}" in text, "реально застрявшая операция должна попасть в алерт"
    assert f"#{sched_id}" not in text, (
        "ОТЛОЖЕННАЯ операция (scheduled_for в будущем) не застряла — ложный алерт "
        "спамил админов чужими owner_id"
    )


def test_watchdog_flags_overdue_pending(pool, monkeypatch):
    """Контроль измерителя: обычная просроченная pending без scheduled_for — ловится."""
    from services import op_worker

    async def _seed():
        await pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER_A)
        return await pool.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,created_at) "
            "VALUES($1,'mass_invite','pending',$2, now()-interval '60 min') RETURNING id",
            OWNER_A, json.dumps({}))

    stuck_id = _run(_seed())
    monkeypatch.setenv("ADMIN_IDS", "424242")
    op_worker._alerted_stuck_ops.clear()
    bot = _FakeBot()
    _run(op_worker._watchdog_alerts(pool, bot))
    assert any(f"#{stuck_id}" in t for _, t in bot.sent), "здоровый случай обязан детектиться"

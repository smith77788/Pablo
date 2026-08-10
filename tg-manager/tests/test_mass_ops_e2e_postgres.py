"""Сквозной прогон массовых операций по НАСТОЯЩЕМУ Postgres. Заглушён только
движок Telethon/Reporter — весь жизненный цикл операции (захват аккаунтов,
per-target лог, инкремент done_items, аудит, финализация) исполняется реальным
воркером против живой БД.

ЗАЧЕМ ОТДЕЛЬНО ОТ ЮНИТ-ТЕСТОВ. Заглушка пула не проверяет типы параметров:
фейковый `execute(q, *a)` глотает что угодно, поэтому ошибки СВЯЗЫВАНИЯ (строка
вместо datetime, dict вместо jsonb, список вместо скаляра) на юнит-тестах
невидимы в принципе. Именно так пережил релиз сломанный
`operation_bus.submit(scheduled_for=...)`, убивавший ВСЕ отложенные операции.
Общий жизненный цикл (`operation_queue`/`operation_log`/`operation_audit`) — один
на все 60+ op_type: биндинг-регресс в нём тихо ломает КАЖДУЮ массовую операцию
сразу. `test_invite_e2e_postgres` стережёт этот слой для инвайта; здесь — для
остальных ядровых массовых действий (join/leave/report), чтобы рефактор общего
цикла не прошёл незаметно.

КАК ЗАПУСТИТЬ — см. docstring `test_invite_e2e_postgres.py` (тот же стенд/DSN).
Без `INFRAGRAM_TEST_DSN` файл пропускается: CI и обычный прогон не ломаются.
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

OWNER = 990777


# ОДИН цикл на весь модуль: соединения asyncpg привязаны к циклу, в котором создан
# пул (те же грабли, что в test_invite_e2e_postgres).
_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


class _Stand:
    def __init__(self, pool):
        self.pool = pool
        self.acc_ids: list[int] = []
        # режим движка: "ok" → успех, "error" → каждый вызов возвращает ошибку.
        self.mode = "ok"
        self.last_op_id: int | None = None

    # ── заглушки движка (единственная граница с Telegram) ────────────────────
    async def _join(self, session, ref, _acc=None):
        if self.mode == "error":
            return {"error": "ChannelPrivateError"}
        return {"title": "T", "members": 5, "channel_id": 1}

    async def _leave(self, session, channel, _acc=None):
        if self.mode == "error":
            return {"ok": False, "error": "ChannelPrivateError"}
        return {"ok": True}

    async def _report_peer(self, session, acc, target, reason, text="", *a, **k):
        if self.mode == "error":
            return {"ok": False, "error": "ReportDeclined"}
        return {"ok": True}

    async def _report_message(self, session, acc, target, ids, reason, text="", *a, **k):
        if self.mode == "error":
            return {"ok": False, "error": "ReportDeclined"}
        return {"ok": True}

    async def seed(self, *, accounts=2):
        p = self.pool
        await p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER)
        await p.execute(
            "DELETE FROM operation_audit WHERE account_id IN "
            "(SELECT id FROM tg_accounts WHERE owner_id=$1)", OWNER)
        await p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER)
        ids = []
        for i in range(accounts):
            ids.append(await p.fetchval(
                "INSERT INTO tg_accounts(owner_id,phone,session_str,is_active,"
                "acc_status,first_name) VALUES($1,$2,$3,TRUE,'active',$4) RETURNING id",
                OWNER, f"+7995{i:07d}", f"sess{i}", f"Acc{i}"))
        self.acc_ids = ids
        return ids

    async def run(self, op_type, params, total=1):
        """Поставить операцию тем же SQL, что и хендлер, и провести воркером."""
        from services import op_worker as w
        p = self.pool
        op_id = await p.fetchval(
            "INSERT INTO operation_queue(owner_id,op_type,status,params,total_items,label) "
            "VALUES($1,$2,'pending',$3,$4,'e2e') RETURNING id",
            OWNER, op_type, json.dumps(params), total)
        rows = await p.fetch(
            "UPDATE operation_queue SET status='running', started_at=now() WHERE id=$1 "
            "RETURNING id, owner_id, op_type, params", op_id)
        self.last_op_id = op_id
        await w._run_op_task(p, None, dict(rows[0]))
        return await p.fetchrow(
            "SELECT status, done_items, total_items, result->>'summary' AS summary, "
            "error_msg FROM operation_queue WHERE id=$1", op_id)

    async def log_counts(self) -> dict:
        rows = await self.pool.fetch(
            "SELECT status, COUNT(*) AS n FROM operation_log WHERE op_id=$1 GROUP BY status",
            self.last_op_id)
        return {r["status"]: r["n"] for r in rows}

    async def audit_count(self) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM operation_audit WHERE operation_id=$1", self.last_op_id)


@pytest.fixture(scope="module")
def stand():
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
                pass  # схемы идемпотентны; частичный сбой не рушит прогон
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        pool = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")

    s = _Stand(pool)

    # Подменяем ТОЛЬКО границу с Telegram; жизненный цикл исполняет реальный воркер.
    # Оригиналы восстанавливаем в teardown — иначе заглушки протекут в другие модули
    # (module-scoped фикстуру нельзя чинить function-scoped monkeypatch'ем).
    from services import account_manager, reporter_engine, op_worker as w
    _orig = {
        "join_channel": account_manager.join_channel,
        "leave_channel": account_manager.leave_channel,
        "report_peer": reporter_engine.report_peer,
        "report_message": reporter_engine.report_message,
        "sleep": w.asyncio.sleep,
    }
    account_manager.join_channel = s._join
    account_manager.leave_channel = s._leave
    reporter_engine.report_peer = s._report_peer
    reporter_engine.report_message = s._report_message

    _sleep = asyncio.sleep

    async def _fast(x):
        return await _sleep(0)
    w.asyncio.sleep = _fast

    yield s

    account_manager.join_channel = _orig["join_channel"]
    account_manager.leave_channel = _orig["leave_channel"]
    reporter_engine.report_peer = _orig["report_peer"]
    reporter_engine.report_message = _orig["report_message"]
    w.asyncio.sleep = _orig["sleep"]
    _run(pool.close())
    global _LOOP
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()


# ── bulk_join ────────────────────────────────────────────────────────────────

def test_bulk_join_happy_path(stand):
    stand.mode = "ok"
    _run(stand.seed(accounts=2))
    links = ["@chan1", "@chan2", "https://t.me/+abcdef"]
    params = {"links": links, "account_ids": stand.acc_ids, "delay_mode": "fast"}
    row = _run(stand.run("bulk_join", params, total=len(links) * len(stand.acc_ids)))
    assert row["status"] == "done", row["error_msg"]
    # каждая ссылка × каждый аккаунт → отдельный шаг, все успешны
    assert row["done_items"] == len(links) * len(stand.acc_ids)
    counts = _run(stand.log_counts())
    assert counts.get("ok") == len(links) * len(stand.acc_ids)
    assert counts.get("error", 0) == 0
    assert _run(stand.audit_count()) > 0


def test_bulk_join_errors_are_logged_not_lost(stand):
    stand.mode = "error"
    _run(stand.seed(accounts=1))
    links = ["@x", "@y"]
    params = {"links": links, "account_ids": stand.acc_ids, "delay_mode": "fast"}
    row = _run(stand.run("bulk_join", params, total=len(links)))
    # Контракт _run_op_task: исполнитель тронул цели, но ни одна не удалась
    # (ok=0, failed>0) → статус нормализуется в 'failed'. Это НЕ «конфигурационный
    # отказ» (ok=0 && failed=0) — предохранитель такое учитывает. Главное: каждая
    # проваленная цель честно залогирована (error-путь биндинга живой), а не потеряна.
    assert row["status"] == "failed", row["error_msg"]
    counts = _run(stand.log_counts())
    assert counts.get("error", 0) == len(links)
    assert counts.get("ok", 0) == 0
    # done_items двигается и на провалах — прогресс операции не «зависает» на нуле
    assert row["done_items"] == len(links)


# ── bulk_leave ───────────────────────────────────────────────────────────────

def test_bulk_leave_happy_path(stand):
    stand.mode = "ok"
    _run(stand.seed(accounts=2))
    channels = ["@a", "@b"]
    params = {"channels": channels, "account_ids": stand.acc_ids, "delay_mode": "fast"}
    row = _run(stand.run("bulk_leave", params, total=len(channels) * len(stand.acc_ids)))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == len(channels) * len(stand.acc_ids)
    counts = _run(stand.log_counts())
    assert counts.get("ok") == len(channels) * len(stand.acc_ids)


# ── mass_report / report_peer ────────────────────────────────────────────────

def test_mass_report_happy_path(stand):
    stand.mode = "ok"
    ids = _run(stand.seed(accounts=2))
    params = {"mode": "peer", "target": "@bad", "reason": "spam", "account_ids": ids}
    row = _run(stand.run("mass_report", params, total=len(ids)))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == len(ids)
    counts = _run(stand.log_counts())
    assert counts.get("ok") == len(ids)


def test_report_peer_happy_path(stand):
    stand.mode = "ok"
    ids = _run(stand.seed(accounts=2))
    # report_peer берёт число аккаунтов из total_items операции
    params = {"target": "@bad2", "reason": "spam"}
    row = _run(stand.run("report_peer", params, total=len(ids)))
    assert row["status"] == "done", row["error_msg"]
    assert row["done_items"] == len(ids)

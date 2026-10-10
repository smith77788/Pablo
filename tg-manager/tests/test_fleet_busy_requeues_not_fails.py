"""Регресс: занятый флот проваливал операцию, а не ставил её в живую очередь.

Первопричина. Когда `try_claim_account(s)` не мог захватить ни одного
аккаунта (все заняты ДРУГОЙ операцией прямо сейчас — не «аккаунтов нет
вовсе»), ~27 разных исполнителей в op_worker.py возвращали
`{"status": "failed", ...}` (в mass_invite — даже `{"status": "done", "ok": 0,
...}` с советом «дождитесь и повторите вручную»). При этом в самом
`_run_op_task` уже был готовый механизм «живой очереди»
(`_requeue_op_no_accounts`): он мягко переставляет операцию на
`scheduled_for = now() + N сек` вместо провала — но срабатывает только на
`status == "requeue"`, а не на "failed"/"done". Поэтому вторая параллельно
запущенная операция на пересекающемся флоте требовала от владельца заметить
провал и нажать «повторить» вручную — выглядело как «параллельные операции
не работают».

Фикс не трогает сам захват (это сознательная защита от AUTH_KEY_DUPLICATED —
один и тот же Telegram-аккаунт не может держать две одновременные сессии, и
это НЕЛЬЗЯ разрешать): меняется только СИГНАЛ при занятости — вместо провала
исполнитель просит переставить себя в очередь, и `_run_op_task` это уже умел.

Реальный Postgres — нужен настоящий INSERT/UPDATE operation_queue и настоящая
аренда аккаунта (`tg_accounts.in_operation`/`op_lease_*`), а не текстовое
сравнение SQL.
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
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = 991501

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _mk():
        p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        files = ["schema.sql"] + sorted(
            glob.glob(os.path.join(ROOT, "schema_v*.sql")),
            key=lambda p_: int(re.search(r"schema_v(\d+)", p_).group(1)))
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(ROOT, f)
            if not os.path.exists(path):
                continue
            try:
                await p.execute(open(path, encoding="utf-8").read())
            except Exception:
                pass
        return p

    try:
        p = _run(_mk())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(p.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    _run(p.close())


@pytest.fixture(autouse=True)
def _clean(pool):
    _run(pool.execute("DELETE FROM operation_queue WHERE owner_id=$1", OWNER))
    _run(pool.execute("DELETE FROM tg_accounts WHERE owner_id=$1", OWNER))
    yield


async def _mk_account(pool, phone="+79990001111") -> int:
    row = await pool.fetchrow(
        "INSERT INTO tg_accounts(owner_id, phone, session_str, acc_status, is_active, "
        "trust_score) VALUES($1,$2,'s','active',TRUE,0.9) RETURNING id",
        OWNER, phone,
    )
    return int(row["id"])


def test_mass_invite_requeues_when_fleet_busy_instead_of_giving_up(pool):
    """Сквозной сценарий бага: аккаунт занят ДРУГОЙ операцией (реальная аренда
    в БД) → mass_invite обязан мягко переставиться в очередь, а не сказать
    «дождитесь и повторите» и не притвориться, что всё done."""
    from services import op_worker as w

    acc_id = _run(_mk_account(pool))
    # init_op_worker_pool пишет в МОДУЛЬНЫЙ глобал op_worker._db_pool — не
    # восстановить его после теста значит подсунуть следующим тестовым файлам
    # в этом же процессе pytest уже ЗАКРЫТЫЙ (после teardown фикстуры pool)
    # пул, и все их claim/release начнут молча фейлиться ("pool is closed").
    _orig_db_pool = w._db_pool
    w.init_op_worker_pool(pool)

    # Другая операция уже держит аккаунт — настоящая аренда в БД.
    claimed = _run(w.try_claim_accounts([acc_id]))
    assert claimed == [acc_id], "не удалось смоделировать занятость — тест не показателен"

    try:
        op_id = _run(pool.fetchval(
            "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, label) "
            "VALUES($1,'mass_invite','pending',$2,1,'busy-fleet test') RETURNING id",
            OWNER, json.dumps({"group": "@g", "source": "import_list", "user_refs": ["@one"]}),
        ))
        row = _run(pool.fetchrow(
            "UPDATE operation_queue SET status='running', started_at=now() WHERE id=$1 "
            "RETURNING id, owner_id, op_type, params", op_id))
        _run(w._run_op_task(pool, None, dict(row)))

        final = _run(pool.fetchrow(
            "SELECT status, scheduled_for, done_items FROM operation_queue WHERE id=$1", op_id))
        assert final["status"] == "pending", (
            f"занятый флот обязан мягко переставить операцию в очередь, а не "
            f"{final['status']!r}"
        )
        assert final["scheduled_for"] is not None, "требуется реальный scheduled_for, не просто pending"
        assert final["done_items"] == 0
    finally:
        _run(w.release_accounts([acc_id]))
        w._db_pool = _orig_db_pool


# ── Ратчет: ни один исполнитель не должен вернуться к провалу на занятости ──

def test_no_busy_fleet_site_still_returns_failed_status():
    """Статическая проверка по всему файлу: любое возвращение с текстом
    "занят(ы) другой операцией" обязано нести status: "requeue", а не
    "failed"/"done" — иначе кто-то скопировал старый (провальный) паттерн."""
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    pattern = re.compile(
        r'"status":\s*"(\w+)"[^}]*?"(?:reason|summary)":\s*"[^"]*занят[ыо]? другой операцией[^"]*"',
        re.S,
    )
    statuses = pattern.findall(src)
    assert statuses, "паттерн не нашёл ни одного вхождения — возможно, текст сообщения поменялся"
    bad = [s for s in statuses if s != "requeue"]
    assert not bad, f"найдены исполнители, всё ещё проваливающие занятый флот: {bad}"


def test_mass_invite_busy_fleet_branch_uses_requeue_not_done():
    src = open(os.path.join(ROOT, "services", "op_worker.py"), encoding="utf-8").read()
    i = src.index("async def _exec_mass_invite")
    seg = src[i:src.index("\nasync def _exec_", i + 1)]
    j = seg.index("Все подходящие аккаунты сейчас заняты")
    branch = seg[max(0, j - 200):j]
    assert '"status": "requeue"' in branch, (
        "занятость флота в mass_invite обязана требовать переоформления в очередь, "
        "а не притворяться завершённой операцией с ok=0"
    )

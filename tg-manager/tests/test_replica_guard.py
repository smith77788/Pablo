"""Страж «одной реплики»: обнаружение нескольких процессов (ось №3 аудита).

Лимиты флуда/параллельности живут в памяти процесса; вторая реплика молча
разгоняет темп по флоту → риск бана. Guard бьёт хартбит и громко предупреждает,
если реплик больше одной. Здесь — что счётчик считает верно, предупреждение
появляется только при >1 и не заглушено, и что guard подключён в main.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from services import replica_guard as rg  # noqa: E402

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")


def test_wired_into_main():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "run_heartbeat_loop" in src, "guard не запускается в main"
    # через _web_resilient — во ВСЕХ ролях (иначе web-реплики не считались бы)
    assert "_web_resilient(\n            \"replica_guard\"" in src or \
           "_web_resilient(" in src and "replica_guard" in src


def test_multi_allowed_reads_env(monkeypatch):
    monkeypatch.delenv("INFRAGRAM_ALLOW_MULTI_REPLICA", raising=False)
    assert rg._multi_allowed() is False
    monkeypatch.setenv("INFRAGRAM_ALLOW_MULTI_REPLICA", "1")
    assert rg._multi_allowed() is True


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
def test_counts_and_warns_on_multiple_replicas(monkeypatch, caplog):
    import asyncpg

    async def _run():
        conn = await asyncpg.connect(DSN)
        try:
            with open(os.path.join(ROOT, "schema_v186.sql"), encoding="utf-8") as f:
                await conn.execute(f.read())
            await conn.execute("DELETE FROM process_heartbeats")

            # одна реплика → нет предупреждения
            await rg.beat(conn, "hostA:1:aaa", "worker")
            assert await rg.active_replica_count(conn) == 1
            monkeypatch.delenv("INFRAGRAM_ALLOW_MULTI_REPLICA", raising=False)
            with caplog.at_level(logging.CRITICAL):
                caplog.clear()
                await rg.check_single_replica(conn)
            assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL]

            # вторая реплика → предупреждение
            await rg.beat(conn, "hostB:2:bbb", "web")
            assert await rg.active_replica_count(conn) == 2
            with caplog.at_level(logging.CRITICAL):
                caplog.clear()
                n = await rg.check_single_replica(conn)
            assert n == 2
            crit = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
            assert crit, "две реплики — а предупреждения нет"
            assert "РЕПЛИК" in crit[0].getMessage()

            # тот же режим, но явно разрешён → молчит
            monkeypatch.setenv("INFRAGRAM_ALLOW_MULTI_REPLICA", "1")
            with caplog.at_level(logging.CRITICAL):
                caplog.clear()
                await rg.check_single_replica(conn)
            assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL], (
                "мульти-режим разрешён явно — предупреждать не должно")

            # beat идемпотентен (upsert): повторный beat не плодит строки
            await rg.beat(conn, "hostA:1:aaa", "worker")
            assert await rg.active_replica_count(conn) == 2
        finally:
            await conn.execute("DELETE FROM process_heartbeats")
            await conn.close()

    asyncio.run(_run())


@pytest.mark.skipif(not DSN, reason="нужен живой Postgres")
def test_stale_replica_not_counted(monkeypatch):
    import asyncpg

    async def _run():
        conn = await asyncpg.connect(DSN)
        try:
            with open(os.path.join(ROOT, "schema_v186.sql"), encoding="utf-8") as f:
                await conn.execute(f.read())
            await conn.execute("DELETE FROM process_heartbeats")
            await rg.beat(conn, "live:1:x", "all")
            # устаревший хартбит (за окном) не должен считаться живой репликой
            await conn.execute(
                "INSERT INTO process_heartbeats(worker_id, role, started_at, last_seen) "
                "VALUES ('dead:9:z','all', now(), now() - interval '10 minutes')")
            assert await rg.active_replica_count(conn) == 1
        finally:
            await conn.execute("DELETE FROM process_heartbeats")
            await conn.close()

    asyncio.run(_run())

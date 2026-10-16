"""Credentials прокси не дублируются в памяти, статистике и логах ошибок."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from services import infra_memory

PROXY = "socks5://private-user:private-password@203.0.113.9:1080"


def test_flush_writes_only_proxy_fingerprint(caplog):
    class Pool:
        def __init__(self):
            self.calls = []

        async def execute(self, *args):
            self.calls.append(args)
            raise RuntimeError("database unavailable")

    infra_memory._proxy_memory.clear()
    infra_memory._dirty_proxy_keys.clear()
    infra_memory.record_proxy_op(PROXY, "join", True)
    pool = Pool()
    with caplog.at_level(logging.WARNING):
        asyncio.run(infra_memory.flush_to_db(pool))

    serialized = repr(pool.calls)
    assert "private-user" not in serialized
    assert "private-password" not in serialized
    assert infra_memory._proxy_identity(PROXY) in serialized
    assert "private-user" not in caplog.text
    assert "private-password" not in caplog.text
    infra_memory._proxy_memory.clear()
    infra_memory._dirty_proxy_keys.clear()


def test_maintenance_compares_fingerprints_not_plaintext():
    source = (Path(__file__).resolve().parents[1] / "services" / "db_maintenance.py").read_text(
        encoding="utf-8"
    )
    assert "proxy_fingerprint" in source
    assert 'decrypt_token(r["proxy_url"])' not in source
    assert 'proxy_fingerprint(r["proxy_url"]) not in _active' in source


def test_startup_migrates_legacy_proxy_key_to_fingerprint():
    proxy_ref = infra_memory._proxy_identity(PROXY)

    class Pool:
        def __init__(self):
            self.proxy_fetches = 0
            self.execute_calls = []

        async def fetch(self, query, *args):
            if "infra_memory_accounts" in query:
                return []
            self.proxy_fetches += 1
            if self.proxy_fetches == 1:
                return [
                    {
                        "proxy_url": PROXY,
                        "action_type": "join",
                        "successes": 7,
                        "failures": 2,
                        "avg_latency_ms": 123.0,
                        "last_success_ts": 10.0,
                        "last_failure_ts": 11.0,
                    }
                ]
            return [
                {
                    "proxy_url": proxy_ref,
                    "action_type": "join",
                    "successes": 7,
                    "failures": 2,
                    "avg_latency_ms": 123.0,
                    "last_success_ts": 10.0,
                    "last_failure_ts": 11.0,
                }
            ]

        async def execute(self, *args):
            self.execute_calls.append(args)

    infra_memory._proxy_memory.clear()
    pool = Pool()
    asyncio.run(infra_memory.load_all_from_db(pool))

    assert pool.proxy_fetches == 2
    assert len(pool.execute_calls) == 1
    assert pool.execute_calls[0][-1] == proxy_ref
    assert (proxy_ref, "join") in infra_memory._proxy_memory
    assert PROXY not in repr(infra_memory._proxy_memory)
    record = infra_memory._proxy_memory[(proxy_ref, "join")]
    assert (record.successes, record.failures) == (7, 2)
    assert (record.flushed_successes, record.flushed_failures) == (7, 2)
    infra_memory._proxy_memory.clear()

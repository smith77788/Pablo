"""Backup Proxy (failover) — раздел 13 паритета Telegram Expert.

Проверяем РЕАЛЬНУЮ логику переназначения аккаунтов с мёртвым прокси на живой
резервный (с соблюдением IP-изоляции) на мок-pool и мок-probe. Плюс: устойчивость
при отсутствии колонки is_backup (лаг миграции) и отсутствии резервов.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from services import proxy_selector as ps


class _FakePool:
    """Минимальный async pool: fetch по совпадению подстроки запроса, execute — лог."""

    def __init__(self, accounts, backups, backup_col_exists=True):
        self._accounts = accounts
        self._backups = backups
        self._backup_col_exists = backup_col_exists
        self.executed: list[tuple] = []

    async def fetch(self, query, *args):
        if "FROM tg_accounts a" in query:
            return self._accounts
        if "is_backup=TRUE" in query:
            if not self._backup_col_exists:
                raise Exception('column "is_backup" does not exist')
            return self._backups
        return []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"


def _run(coro):
    return asyncio.run(coro)


# url→(alive, ip) карта для мок-probe/extract
def _make_patches(url_alive: dict, url_ip: dict):
    async def fake_probe(url, timeout=10.0):
        return {"ok": bool(url_alive.get(url, False))}

    def fake_ip(url):
        return url_ip.get(url)

    return fake_probe, fake_ip


def test_dead_primary_reassigned_to_healthy_backup():
    accounts = [{"account_id": 1, "proxy_id": 10, "proxy_url": "socks5://dead"}]
    backups = [{"id": 20, "proxy_url": "socks5://backup"}]
    pool = _FakePool(accounts, backups)
    probe, ip = _make_patches(
        {"socks5://dead": False, "socks5://backup": True},
        {"socks5://dead": "1.1.1.1", "socks5://backup": "2.2.2.2"},
    )
    with patch.object(ps, "probe_proxy", probe), patch.object(ps, "extract_ip_from_proxy", ip):
        res = _run(ps.failover_dead_proxies(pool, owner_id=99))
    assert res["checked"] == 1 and res["healthy"] == 0
    assert res["reassigned"] == [{"account_id": 1, "from_proxy_id": 10, "to_proxy_id": 20}]
    assert res["still_dead_no_backup"] == []
    # реально выполнен UPDATE tg_accounts на новый proxy_id
    assert any("UPDATE tg_accounts SET proxy_id" in q for q, _ in pool.executed)


def test_dead_primary_no_backup_reports_stuck():
    accounts = [{"account_id": 1, "proxy_id": 10, "proxy_url": "socks5://dead"}]
    pool = _FakePool(accounts, [])
    probe, ip = _make_patches({"socks5://dead": False}, {"socks5://dead": "1.1.1.1"})
    with patch.object(ps, "probe_proxy", probe), patch.object(ps, "extract_ip_from_proxy", ip):
        res = _run(ps.failover_dead_proxies(pool, owner_id=99))
    assert res["reassigned"] == [] and res["still_dead_no_backup"] == [1]
    assert not any("UPDATE tg_accounts SET proxy_id" in q for q, _ in pool.executed)


def test_healthy_primary_untouched():
    accounts = [{"account_id": 1, "proxy_id": 10, "proxy_url": "socks5://live"}]
    backups = [{"id": 20, "proxy_url": "socks5://backup"}]
    pool = _FakePool(accounts, backups)
    probe, ip = _make_patches(
        {"socks5://live": True, "socks5://backup": True},
        {"socks5://live": "1.1.1.1", "socks5://backup": "2.2.2.2"},
    )
    with patch.object(ps, "probe_proxy", probe), patch.object(ps, "extract_ip_from_proxy", ip):
        res = _run(ps.failover_dead_proxies(pool, owner_id=99))
    assert res["healthy"] == 1 and res["reassigned"] == []
    assert not any("UPDATE tg_accounts SET proxy_id" in q for q, _ in pool.executed)


def test_backup_on_used_ip_skipped_isolation():
    # резервный делит IP с уже активным аккаунтом → нельзя брать (изоляция)
    accounts = [
        {"account_id": 1, "proxy_id": 10, "proxy_url": "socks5://dead"},
        {"account_id": 2, "proxy_id": 11, "proxy_url": "socks5://other"},
    ]
    backups = [{"id": 20, "proxy_url": "socks5://backup"}]
    pool = _FakePool(accounts, backups)
    probe, ip = _make_patches(
        {"socks5://dead": False, "socks5://other": True, "socks5://backup": True},
        # backup имеет тот же IP, что и уже используемый socks5://other
        {"socks5://dead": "1.1.1.1", "socks5://other": "9.9.9.9", "socks5://backup": "9.9.9.9"},
    )
    with patch.object(ps, "probe_proxy", probe), patch.object(ps, "extract_ip_from_proxy", ip):
        res = _run(ps.failover_dead_proxies(pool, owner_id=99))
    assert res["backups_healthy"] == 0  # резерв отфильтрован по изоляции
    assert res["still_dead_no_backup"] == [1] and res["reassigned"] == []


def test_missing_is_backup_column_graceful():
    accounts = [{"account_id": 1, "proxy_id": 10, "proxy_url": "socks5://dead"}]
    pool = _FakePool(accounts, [], backup_col_exists=False)
    probe, ip = _make_patches({"socks5://dead": False}, {"socks5://dead": "1.1.1.1"})
    with patch.object(ps, "probe_proxy", probe), patch.object(ps, "extract_ip_from_proxy", ip):
        res = _run(ps.failover_dead_proxies(pool, owner_id=99))
    # не падает; резервов нет → аккаунт помечен как без резерва
    assert res["backups_available"] == 0 and res["still_dead_no_backup"] == [1]


def test_route_and_engine_wired():
    import inspect
    from services import mini_app_api
    src = inspect.getsource(mini_app_api)
    assert 'app.router.add_post("/api/miniapp/proxy/failover", proxy_failover)' in src
    assert 'app.router.add_post("/api/miniapp/proxy/{proxy_id}/backup", proxy_toggle_backup)' in src
    assert hasattr(ps, "failover_dead_proxies")

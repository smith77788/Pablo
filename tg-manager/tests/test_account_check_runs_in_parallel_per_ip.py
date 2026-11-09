"""Массовая проверка аккаунтов идёт параллельно, но не больше одного на IP.

Замер 08.11.2026: проверка шла строго по одному — подключение, get_me,
диалог с @SpamBot и пауза 2,5 с, не меньше 5 с на аккаунт. 1 000 аккаунтов
проверялись около полутора часов, 5 000 — около семи. Требование М3:
1 000 аккаунтов на разных прокси — не дольше 15 минут.

Параллелить можно только разные выходы в сеть. Аккаунты за одним прокси (и
все без прокси — это IP сервера) обязаны идти строго друг за другом: иначе
один IP увидел бы сразу несколько сессий, а это ровно тот сигнал, за который
Telegram ограничивает. Здесь стережём:
  * разные прокси проверяются одновременно (и быстрее по стене);
  * на одном прокси и на «без прокси» одновременно не бывает двух проверок;
  * отмена останавливает все полосы и честно называет, сколько проверено;
  * CHECK_HEALTH_PARALLEL=1 возвращает прежний порядок по одному;
  * журнал операции получает запись по КАЖДОМУ аккаунту с его номером.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from services import op_worker


class _Pool:
    def __init__(self, accounts):
        self._accounts = accounts
        self.journal = []

    async def fetch(self, q, *a):
        return self._accounts

    async def execute(self, q, *a):
        if "operation_log" in q:
            self.journal.append(a)
        return "UPDATE 1"

    async def fetchval(self, q, *a):
        return 0


def _acc(i, proxy):
    return {"id": i, "session_str": "s", "first_name": f"A{i}", "phone": "+1",
            "username": "u", "proxy_url": proxy}


class _Tracker:
    """Подменная проверка: считает одновременные проверки на каждом выходе."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.live: dict[str, int] = {}
        self.max_per_ip: dict[str, int] = {}
        self.max_total = 0
        self.total = 0

    async def __call__(self, session_str, _acc=None, check_spambot=True):
        ip = (_acc.get("proxy_url") or "direct")
        self.live[ip] = self.live.get(ip, 0) + 1
        self.total += 1
        self.max_per_ip[ip] = max(self.max_per_ip.get(ip, 0), self.live[ip])
        self.max_total = max(self.max_total, self.total)
        await asyncio.sleep(self.delay)
        self.live[ip] -= 1
        self.total -= 1
        return {"status": "active", "reason": "ok"}


def _run(accounts, tracker, cancelled=None, env=None):
    if cancelled is None:
        cancelled = AsyncMock(return_value=False)
    pool = _Pool(accounts)
    with patch("services.account_manager.check_account_status_full", tracker), \
         patch("services.account_manager.should_persist_account_status",
               lambda *a, **k: False), \
         patch.object(op_worker, "_is_cancelled", cancelled), \
         patch.dict("os.environ", env or {}):
        res = asyncio.run(op_worker._exec_check_accounts_health(
            pool, None, 5, 99, {"check_spambot": True}))
    return res, pool


def test_different_proxies_run_together_same_ip_never_does():
    accs = ([_acc(i, f"socks5://p{i % 4}:1080") for i in range(12)]
            + [_acc(100 + i, None) for i in range(3)])
    t = _Tracker()
    res, pool = _run(accs, t)
    assert res["status"] == "done" and res["checked"] == 15
    assert t.max_total > 1, "проверка по-прежнему идёт строго по одному"
    assert all(v == 1 for v in t.max_per_ip.values()), (
        f"на одном IP одновременно шло несколько проверок: {t.max_per_ip}")
    assert sorted(a[1] for a in pool.journal) == list(range(1, 16)), (
        "журнал операции потерял запись или номер аккаунта")


def test_accounts_without_proxy_share_the_server_ip():
    t = _Tracker(delay=0.01)
    _run([_acc(i, None) for i in range(5)], t)
    assert t.max_per_ip == {"direct": 1}, "аккаунты без прокси пошли с IP сервера разом"


def test_parallel_one_restores_strict_order():
    t = _Tracker(delay=0.01)
    _run([_acc(i, f"socks5://p{i}:1080") for i in range(5)], t,
         env={"CHECK_HEALTH_PARALLEL": "1"})
    assert t.max_total == 1


def test_wall_time_drops_with_distinct_proxies():
    import time
    accs = [_acc(i, f"socks5://p{i}:1080") for i in range(16)]
    t0 = time.perf_counter()
    _run(accs, _Tracker(delay=0.05), env={"CHECK_HEALTH_PARALLEL": "8"})
    wall = time.perf_counter() - t0
    assert wall < 16 * 0.05 / 2, f"16 проверок по 50 мс на 16 прокси шли {wall:.2f} с"


def test_cancel_stops_every_lane():
    calls = {"n": 0}

    async def cancelled(pool, op_id):
        calls["n"] += 1
        return calls["n"] > 4

    t = _Tracker(delay=0.01)
    res, _ = _run([_acc(i, f"socks5://p{i % 3}:1080") for i in range(30)], t,
                  cancelled=cancelled)
    assert res["status"] == "cancelled"
    assert res["checked"] < 30
    assert f"{res['checked']}/30" in res["summary"]

"""Проверка прав аккаунтов в чате идёт параллельно, а не по очереди.

channel_admin_status — это живое подключение к Telegram: секунды в норме и до
25 секунд на мёртвой сессии. Восемь аккаунтов по очереди складывали эти паузы
в одну, и в худшем случае HTTP-запрос обрывался по таймауту раньше, чем экран
получал ответ. Порядок результатов обязан сохраняться: «первый подходящий»
аккаунт должен остаться тем же, что и при обходе по очереди.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from services import mini_app_api as m


class _FakeEngine:
    """Подменяет services.mass_inviter_engine с управляемыми задержками."""

    def __init__(self, plan: dict[int, tuple[float, dict]]):
        self.plan = plan
        self.peak = 0
        self._live = 0

    async def channel_admin_status(self, session_str, acc, group):
        self._live += 1
        self.peak = max(self.peak, self._live)
        try:
            delay, result = self.plan[int(acc["id"])]
            if isinstance(result, Exception):
                await asyncio.sleep(delay)
                raise result
            await asyncio.sleep(delay)
            return result
        finally:
            self._live -= 1


@pytest.fixture
def engine(monkeypatch):
    import sys
    import types

    def _install(plan):
        fake = _FakeEngine(plan)
        mod = types.ModuleType("services.mass_inviter_engine")
        mod.channel_admin_status = fake.channel_admin_status
        monkeypatch.setitem(sys.modules, "services.mass_inviter_engine", mod)
        return fake

    return _install


def _rows(n):
    return [{"id": i, "session_str": f"s{i}", "phone": f"+7000000000{i}"}
            for i in range(1, n + 1)]


def test_scan_runs_in_parallel_not_sequentially(engine):
    fake = engine({i: (0.2, {"ok": True}) for i in range(1, 9)})

    started = time.monotonic()
    res = asyncio.run(m._scan_admin_status(_rows(8), "@chat", concurrency=8))
    elapsed = time.monotonic() - started

    assert len(res) == 8 and all(r["ok"] for r in res)
    # По очереди было бы ~1.6 с; параллельно — около одной задержки.
    assert elapsed < 0.9, f"обход всё ещё последовательный: {elapsed:.2f}s"
    assert fake.peak > 1


def test_scan_respects_concurrency_limit(engine):
    fake = engine({i: (0.05, {"ok": True}) for i in range(1, 9)})
    asyncio.run(m._scan_admin_status(_rows(8), "@chat", concurrency=3))
    assert fake.peak <= 3, "одновременных подключений больше разрешённого"


def test_scan_keeps_row_order(engine):
    fake = engine({
        1: (0.20, {"ok": True, "tag": "first"}),
        2: (0.01, {"ok": True, "tag": "second"}),
        3: (0.10, {"ok": True, "tag": "third"}),
    })
    res = asyncio.run(m._scan_admin_status(_rows(3), "@chat", concurrency=3))
    assert [r["tag"] for r in res] == ["first", "second", "third"], \
        "быстрый аккаунт не должен опережать порядок строк"


def test_failed_and_timed_out_accounts_do_not_sink_the_scan(engine):
    engine({
        1: (0.01, RuntimeError("сессия мертва")),
        2: (0.5, {"ok": True, "tag": "slow"}),      # уйдёт в таймаут
        3: (0.01, {"ok": True, "tag": "fine"}),
    })
    res = asyncio.run(m._scan_admin_status(_rows(3), "@chat",
                                           timeout=0.2, concurrency=3))
    assert res[0] == {}, "упавший аккаунт — пустой результат, а не исключение"
    assert res[1] == {}, "не ответивший вовремя — тоже пустой"
    assert res[2].get("tag") == "fine"


def test_empty_rows_do_not_touch_telegram(monkeypatch):
    import sys

    def _boom(*a, **kw):
        raise AssertionError("к Telegram обращаться не за чем")

    import types
    mod = types.ModuleType("services.mass_inviter_engine")
    mod.channel_admin_status = _boom
    monkeypatch.setitem(sys.modules, "services.mass_inviter_engine", mod)
    assert asyncio.run(m._scan_admin_status([], "@chat")) == []


def test_non_dict_answer_is_treated_as_no_answer(engine):
    engine({1: (0.01, None)})
    res = asyncio.run(m._scan_admin_status(_rows(1), "@chat"))
    assert res == [{}]

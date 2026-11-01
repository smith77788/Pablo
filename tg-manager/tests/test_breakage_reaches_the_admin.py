"""Поломка доходит до админа сообщением, а не остаётся в логе (D27).

ЧТО БЫЛО. `service_supervisor.supervise` при падении фонового сервиса писал
`log.error` и молча перезапускал его; `ErrorMonitorMiddleware` при всплеске
ошибок в обработчиках бота писал `log.warning`. Ни одно из двух не доходило до
человека: сервис, падающий каждые 30 секунд, выглядел снаружи как «что-то
медленно» — до первой жалобы клиента.

ЧТО СТАЛО. Оба места зовут `platform_alert`: получатели — те же, что у
остальных платформенных алертов, анти-повтор персистентный, на каждого
получателя свой, сбой доставки снимает отметку.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from services import platform_alert, service_supervisor

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(c):
    return asyncio.run(c)


class _Bot:
    def __init__(self, fail_for=()):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_for:
            raise RuntimeError("blocked")
        self.sent.append((chat_id, text))


@pytest.fixture
def admins(monkeypatch):
    from bot.utils import subscription
    from database import db

    seen: dict[tuple[int, str], bool] = {}
    forgotten: list[tuple[int, str]] = []

    async def _recipients(pool):
        return {1, 2}

    async def _dedup_ok(pool, uid, key, cooldown):
        if (uid, key) in seen:
            return False
        seen[(uid, key)] = True
        return True

    async def _forget(pool, uid, key):
        seen.pop((uid, key), None)
        forgotten.append((uid, key))

    monkeypatch.setattr(subscription, "platform_alert_recipients", _recipients)
    monkeypatch.setattr(db, "notify_dedup_ok", _dedup_ok)
    monkeypatch.setattr(db, "notify_dedup_forget", _forget)
    return forgotten


def test_alert_reaches_every_admin_once(admins):
    bot = _Bot()
    alert = platform_alert.make_alerter(object(), bot)
    _run(alert("k", "текст"))
    _run(alert("k", "текст"))           # повтор в окне — не уходит
    assert sorted(c for c, _ in bot.sent) == [1, 2]


def test_failed_delivery_frees_the_window(admins):
    bot = _Bot(fail_for={2})
    _run(platform_alert.make_alerter(object(), bot)("k", "текст"))
    assert [c for c, _ in bot.sent] == [1]
    assert admins == [(2, "k")]


def test_alerter_never_raises(monkeypatch):
    from bot.utils import subscription

    async def _boom(pool):
        raise RuntimeError("db down")

    monkeypatch.setattr(subscription, "platform_alert_recipients", _boom)
    _run(platform_alert.make_alerter(object(), _Bot())("k", "т"))


def test_crashed_service_is_reported(monkeypatch):
    got: list[tuple[str, str]] = []

    async def _alert(key, text):
        got.append((key, text))

    calls = {"n": 0}

    async def _svc():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("<сломалось>")
        return None                      # второй запуск — «выключен»

    _run(service_supervisor.supervise("scheduler", _svc, restart_delay=0,
                                      alert=_alert))
    assert len(got) == 1
    key, text = got[0]
    assert key == "svc-crash:scheduler"
    assert "scheduler" in text and "ValueError" in text
    assert "&lt;сломалось&gt;" in text   # HTML экранирован


def test_network_service_returning_is_reported():
    got: list[str] = []

    async def _alert(key, text):
        got.append(key)
        raise asyncio.CancelledError     # останавливаем присмотр после алерта

    async def _svc():
        return None

    with pytest.raises(asyncio.CancelledError):
        _run(service_supervisor.supervise("http", _svc, restart_delay=0,
                                          restart_on_return=True, alert=_alert))
    assert got == ["svc-returned:http"]


def test_main_wires_alert_into_both_supervisors():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "platform_alert.make_alerter(pool, bot)" in src
    assert src.count("alert=_svc_alert") == 2


def test_error_spike_is_reported(admins):
    from services import error_monitor as em

    mon = em.ErrorMonitor(window_seconds=300, alert_threshold=2)
    bot = _Bot()

    class _Ev:
        from_user = None

    async def _handler(event, data):
        raise RuntimeError("<boom>")

    mw = em.ErrorMonitorMiddleware()

    async def _go():
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await mw(_handler, _Ev(), {"pool": object(), "bot": bot})

    import unittest.mock as um
    with um.patch.object(em, "get_error_monitor", lambda: mon):
        _run(_go())
    assert sorted(c for c, _ in bot.sent) == [1, 2]
    text = bot.sent[0][1]
    assert "Всплеск ошибок" in text and "&lt;boom&gt;" in text

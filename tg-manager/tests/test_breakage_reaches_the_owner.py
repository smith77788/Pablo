"""D27: владелец узнаёт о поломке из сообщения, а не из лога.

ЧТО ЛОМАЛОСЬ. `service_supervisor` молча перезапускал упавший фоновый сервис,
`error_monitor` при всплеске ошибок только писал `log.warning`. Владелец
узнавал о поломке от пользователя.

ЧТО ПРОВЕРЯЕМ.
* падение фонового сервиса уходит админам тревогой с именем сервиса;
* всплеск ошибок в хендлерах уходит админам тревогой с кодом ошибки;
* секреты из текста ошибки в тревогу не попадают;
* анти-повтор держит одну тревогу на ключ, сбой отправки снимает отметку;
* сбой самой тревоги не роняет присмотр.
"""
from __future__ import annotations

import asyncio

import pytest

from services import error_monitor, platform_alerts, service_supervisor


class _Bot:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id, text, parse_mode=None):
        if self.fail:
            raise RuntimeError("telegram недоступен")
        self.sent.append((chat_id, text))


@pytest.fixture
def wired(monkeypatch):
    """Привязка к фальшивому боту, один админ, анти-повтор в памяти."""
    bot = _Bot()
    seen: set[tuple[int, str]] = set()
    forgotten: list[tuple[int, str]] = []

    async def _recipients(pool):
        return {42}

    async def _dedup_ok(pool, uid, key, cooldown):
        if (uid, key) in seen:
            return False
        seen.add((uid, key))
        return True

    async def _forget(pool, uid, key):
        seen.discard((uid, key))
        forgotten.append((uid, key))

    from bot.utils import subscription
    from database import db

    monkeypatch.setattr(subscription, "platform_alert_recipients", _recipients)
    monkeypatch.setattr(db, "notify_dedup_ok", _dedup_ok)
    monkeypatch.setattr(db, "notify_dedup_forget", _forget)
    platform_alerts.bind(object(), bot)
    yield bot, forgotten
    platform_alerts.bind(None, None)


@pytest.mark.asyncio
async def test_crashed_background_service_alerts_the_admin(wired, monkeypatch):
    bot, _ = wired
    calls = 0

    async def _service():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("сломалось: 1BVtsOK4Bu" + "A" * 300)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service_supervisor.supervise("proxy_watchdog", _service, restart_delay=0)

    assert len(bot.sent) == 1, "падение фонового сервиса не дошло до админа"
    chat, text = bot.sent[0]
    assert chat == 42
    assert "proxy_watchdog" in text
    assert "ValueError" in text
    assert "A" * 300 not in text, "строка сессии ушла в тревогу"


@pytest.mark.asyncio
async def test_crash_loop_sends_one_alert_not_a_flood(wired):
    bot, _ = wired
    calls = 0

    async def _service():
        nonlocal calls
        calls += 1
        if calls <= 5:
            raise RuntimeError("снова")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service_supervisor.supervise("rehab", _service, restart_delay=0)
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_alert_failure_never_stops_the_supervisor(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("тревога сломана")

    monkeypatch.setattr(platform_alerts, "alert", _boom)
    calls = 0

    async def _service():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("упал")
        return

    await asyncio.wait_for(
        service_supervisor.supervise("db_backup", _service, restart_delay=0), 2)
    assert calls == 2, "сбой тревоги остановил перезапуск сервиса"


@pytest.mark.asyncio
async def test_error_spike_alerts_the_admin(wired):
    bot, _ = wired
    monitor = error_monitor.ErrorMonitor(window_seconds=300, alert_threshold=3)
    error_monitor._default_monitor = monitor
    mw = error_monitor.ErrorMonitorMiddleware()

    async def _handler(event, data):
        raise RuntimeError("база недоступна")

    class _Event:
        from_user = None

    try:
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await mw(_handler, _Event(), {})
        await asyncio.gather(*list(platform_alerts._pending))
    finally:
        error_monitor._default_monitor = None

    assert len(bot.sent) == 1, "всплеск ошибок остался в логе"
    assert "Всплеск ошибок" in bot.sent[0][1]


@pytest.mark.asyncio
async def test_failed_delivery_releases_the_dedup_mark(wired):
    bot, forgotten = wired
    bot.fail = True
    assert await platform_alerts.alert("k", "текст") == 0
    assert forgotten == [(42, "k")], "недоставленная тревога заглушила следующую"


@pytest.mark.asyncio
async def test_unbound_alert_is_logged_not_raised():
    platform_alerts.bind(None, None)
    assert await platform_alerts.alert("k", "текст") == 0

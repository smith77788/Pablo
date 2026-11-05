"""Защитные тревоги не должны замолкать молча: предупредить, пока ещё есть кому.

РАЗРЫВ. Весь слой защитных отказов устроен так: операция отказывается делать
необратимое действие, считает это счётчиком, а сторож
(`_watchdog_protective_failures`) превращает счётчик в сообщение админу.
Получатель — последнее звено, и берётся он из `platform_alert_recipients`:
`ADMIN_IDS` ∪ живые сессии админки.

`ADMIN_IDS` в обычной установке не задан — так прямо и написано в докстринге
`platform_alert_recipients`. Значит единственный получатель всех защитных
тревог — сессия админки, а она живёт `ADMIN_SESSION_TTL_HOURS` (по умолчанию
30 дней) с последнего входа секретной фразой и истекает сама.

В день истечения весь слой становится немым. Счётчик
`infragram_protective_alerts_no_recipient_total` это зафиксирует, но прочитать
его некому — ровно потому, что читателя больше нет. Предупредить об этом
можно только ЗАРАНЕЕ, пока доступ ещё жив и сообщение ещё доставляется.

Поэтому сторож смотрит вперёд: если постоянного получателя нет, а последняя
живая сессия истекает в ближайшие дни, он один раз в сутки говорит об этом
теми же словами и предлагает оба выхода — снова войти в админку (продлить) или
задать `ADMIN_IDS` раз и навсегда.
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "services", "op_worker.py")


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((int(chat_id), str(text)))


class _Pool:
    """Пул с одной строкой: когда истекает последняя живая сессия админки."""

    def __init__(self, expires_in_days=None):
        self.expires_in_days = expires_in_days

    async def fetchrow(self, q, *a, **kw):
        if "platform_admin_sessions" in q:
            if self.expires_in_days is None:
                return {"last": None}
            return {"last": _dt.datetime.now(_dt.timezone.utc)
                    + _dt.timedelta(days=self.expires_in_days)}
        return None

    async def fetch(self, *a, **kw):
        return []

    async def execute(self, *a, **kw):
        return "UPDATE 1"

    async def fetchval(self, *a, **kw):
        return None


@pytest.fixture
def ow(monkeypatch):
    from services import op_worker
    yield op_worker


def _patch_recipients(monkeypatch, ow, *, permanent: set, session: set):
    """Подменить обе двери получателей: постоянных и сессионных."""
    import bot.utils.subscription as _sub
    import bot.handlers.admin as _adm

    async def _recips(pool):
        return set(permanent) | set(session)

    async def _live(pool):
        return set(session)

    monkeypatch.setattr(_sub, "platform_alert_recipients", _recips)
    monkeypatch.setattr(_adm, "live_session_admins", _live)

    async def _dedup_ok(pool, uid, key, ttl):
        return True

    from database import db as _db
    monkeypatch.setattr(_db, "notify_dedup_ok", _dedup_ok)


def test_it_warns_while_the_warning_can_still_be_delivered(ow, monkeypatch):
    """Постоянного получателя нет, сессия истекает на днях — предупреждение уходит."""
    _patch_recipients(monkeypatch, ow, permanent=set(), session={777})
    bot = _Bot()
    asyncio.run(ow._watchdog_alert_channel_expiry(_Pool(expires_in_days=3), bot))
    assert bot.sent, (
        "последняя живая сессия админки истекает через три дня, постоянного "
        "получателя нет — после истечения все защитные тревоги станут немыми, "
        "и предупредить будет уже некого, но сторож молчит"
    )
    text = bot.sent[0][1]
    assert "ADMIN_IDS" in text, "в предупреждении нет постоянного выхода (ADMIN_IDS)"
    assert "админк" in text.lower(), "в предупреждении нет второго выхода — войти снова"


def test_it_is_silent_when_a_permanent_recipient_exists(ow, monkeypatch):
    """Самопроверка: ADMIN_IDS задан — канал не истекает, пугать нечем."""
    _patch_recipients(monkeypatch, ow, permanent={42}, session={777})
    bot = _Bot()
    asyncio.run(ow._watchdog_alert_channel_expiry(_Pool(expires_in_days=1), bot))
    assert not bot.sent, (
        "есть постоянный получатель, тревоги не замолкнут — предупреждение лишнее"
    )


def test_it_is_silent_while_the_access_is_far_from_expiring(ow, monkeypatch):
    """Самопроверка: до истечения ещё месяц — не шумим."""
    _patch_recipients(monkeypatch, ow, permanent=set(), session={777})
    bot = _Bot()
    asyncio.run(ow._watchdog_alert_channel_expiry(_Pool(expires_in_days=29), bot))
    assert not bot.sent


def test_it_is_silent_when_there_is_nobody_left(ow, monkeypatch):
    """Получателей уже нет — это случай другого счётчика, а не этого сторожа."""
    _patch_recipients(monkeypatch, ow, permanent=set(), session=set())
    bot = _Bot()
    asyncio.run(ow._watchdog_alert_channel_expiry(_Pool(expires_in_days=None), bot))
    assert not bot.sent


def test_the_warning_does_not_repeat_every_hour(ow, monkeypatch):
    """Анти-повтор: предупреждение идёт через ту же дверь notify_dedup_ok."""
    _patch_recipients(monkeypatch, ow, permanent=set(), session={777})
    calls = []

    async def _dedup_once(pool, uid, key, ttl):
        calls.append((int(uid), str(key), float(ttl)))
        return len(calls) == 1

    from database import db as _db
    monkeypatch.setattr(_db, "notify_dedup_ok", _dedup_once)
    bot = _Bot()
    pool = _Pool(expires_in_days=2)
    asyncio.run(ow._watchdog_alert_channel_expiry(pool, bot))
    asyncio.run(ow._watchdog_alert_channel_expiry(pool, bot))
    assert len(bot.sent) == 1, "предупреждение уходит каждый круг сторожа"
    assert calls and calls[0][2] >= 12 * 3600, (
        "окно анти-повтора короче полусуток — владелец получит это каждый час"
    )


def test_a_broken_bot_does_not_break_the_watchdog(ow, monkeypatch):
    """Сбой отправки не роняет сторож: он ходит в общем цикле воркера."""
    _patch_recipients(monkeypatch, ow, permanent=set(), session={777})

    class _Broken(_Bot):
        async def send_message(self, *a, **kw):
            raise RuntimeError("Telegram недоступен")

    asyncio.run(ow._watchdog_alert_channel_expiry(_Pool(expires_in_days=1), _Broken()))


def test_the_worker_actually_runs_this_watchdog():
    """Сторож, которого никто не вызывает, не сторож."""
    with open(SRC, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    run = next((n for n in ast.walk(tree)
                if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
                and n.name == "run"), None)
    assert run is not None
    called = any(
        isinstance(n, ast.Call)
        and getattr(n.func, "id", getattr(n.func, "attr", "")) == "_watchdog_alert_channel_expiry"
        for n in ast.walk(run))
    assert called, (
        "_watchdog_alert_channel_expiry не вызывается из run — предупреждение "
        "о скорой немоте защитных тревог не случится никогда"
    )

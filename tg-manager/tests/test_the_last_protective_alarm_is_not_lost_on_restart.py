"""Сбой защиты в последние минуты перед остановкой обязан доехать до владельца.

РАЗРЫВ. Счётчики защиты живут в памяти процесса — так и написано над
`_PROTECTIVE_FAILURE_COUNTERS`. Доносит их владельцу словами сторож
`_watchdog_protective_failures`, и ходит он раз в ~5 минут (каждый 30-й круг
поллера). Значит сбой защитной записи, случившийся в последние минуты перед
остановкой, не доезжал НИКОГДА: процесс умирал вместе со счётчиками, а новый
начинал с нуля.

ПОЧЕМУ ЭТО НЕ РЕДКИЙ УГОЛ. Сбои защитных записей как раз к остановке и
тяготеют: рестарт платформы давит на базу, а `_reset_stale_running` на старте
пишет в неё первым делом. То есть окно потери совпадает с окном, где потеря
вероятнее всего. Автодеплой означает рестарт на каждый пуш.

ПОЧЕМУ СЛИВ НЕ СПАМИТ. Окно анти-повтора у этих тревог лежит в БД
(`notify_dedup_ok`, 3 часа) и рестарт переживает. Поэтому база отсчёта на
сливе пустая — «доложи всё, о чём ещё не докладывали»: о чём докладывали,
отсечёт окно, а не наша память.

ПОЧЕМУ СЛИВ ПОСЛЕДНИЙ И ОГРАНИЧЕН ПО ВРЕМЕНИ. Возврат операций в очередь и
снятие аренд аккаунтов важнее тревоги: без них флот стоит до истечения TTL.
Поэтому слив идёт после них и под `wait_for` — платформа убивает контейнер по
своему таймауту, и зависшая отправка забрала бы остаток уборки.
"""
from __future__ import annotations

import asyncio
import os

import pytest


@pytest.fixture
def ow():
    from services import op_worker
    yield op_worker


@pytest.fixture(autouse=True)
def _clean(ow):
    from services import metrics as _m
    _m.reset()
    _m.set_shutting_down(False)
    ow._shutting_down = False
    ow._active_op_ids.clear()
    ow._active_op_tasks.clear()
    ow._accounts_in_use.clear()
    yield
    _m.reset()
    _m.set_shutting_down(False)
    ow._shutting_down = False
    ow._active_op_ids.clear()
    ow._active_op_tasks.clear()
    ow._accounts_in_use.clear()


class _Pool:
    async def execute(self, q, *a):
        return "UPDATE 0"

    async def fetch(self, q, *a):
        return []

    async def fetchrow(self, q, *a):
        return None

    async def fetchval(self, q, *a):
        return None


class _Bot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _arm(ow, monkeypatch, *, dedup_ok=True):
    """Поднять защитный счётчик и расчистить путь доставки."""
    from services import metrics as _m
    _m.inc("infragram_journal_write_failures_total", {"where": "journal"}, 2)

    import bot.utils.subscription as _subs
    async def _recipients(pool):
        return {777}
    monkeypatch.setattr(_subs, "platform_alert_recipients", _recipients)

    async def _ok(pool, aid, key, ttl):
        return dedup_ok
    async def _forget(pool, aid, key):
        return None
    monkeypatch.setattr(ow.db, "notify_dedup_ok", _ok, raising=False)
    monkeypatch.setattr(ow.db, "notify_dedup_forget", _forget, raising=False)

    async def _release(ids):
        return None
    monkeypatch.setattr(ow, "_db_release", _release)


# ── Главное ──────────────────────────────────────────────────────────────────

def test_shutdown_delivers_the_pending_protective_alarm(ow, monkeypatch):
    """Иначе о повторных реальных действиях владелец узнаёт только из жалоб."""
    _arm(ow, monkeypatch)
    tg = _Bot()

    asyncio.run(ow.shutdown(_Pool(), grace_s=0.0, bot=tg))

    assert tg.sent, "последняя защитная тревога не ушла владельцу на остановке"
    text = "\n".join(t for _aid, t in tg.sent)
    assert "Защита не записалась" in text, f"ушло что-то другое: {text[:200]!r}"


def test_shutdown_without_a_bot_still_cleans_up(ow, monkeypatch):
    """Слив — не условие уборки: без бота остановка обязана пройти как раньше."""
    _arm(ow, monkeypatch)

    res = asyncio.run(ow.shutdown(_Pool(), grace_s=0.0))

    assert isinstance(res, dict) and "requeued" in res, f"уборка сломалась: {res}"


def test_a_hanging_alert_does_not_hold_the_shutdown(ow, monkeypatch):
    """Платформа убьёт контейнер по своему таймауту — слив обязан быть с рамкой."""
    _arm(ow, monkeypatch)

    async def _hang(pool, bot, seen):
        await asyncio.sleep(3600)

    monkeypatch.setattr(ow, "_watchdog_protective_failures", _hang)
    monkeypatch.setattr(ow, "_SHUTDOWN_ALERT_FLUSH_S", 0.2)

    async def _go():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await ow.shutdown(_Pool(), grace_s=0.0, bot=_Bot())
        return loop.time() - started

    spent = asyncio.run(_go())
    assert spent < 5.0, f"остановка ждала зависший слив {spent:.1f}с"


def test_nothing_is_sent_when_no_protection_failed(ow, monkeypatch):
    """Самопроверка: остановка без сбоев защиты владельца не будит."""
    import bot.utils.subscription as _subs
    async def _recipients(pool):
        return {777}
    monkeypatch.setattr(_subs, "platform_alert_recipients", _recipients)

    async def _release(ids):
        return None
    monkeypatch.setattr(ow, "_db_release", _release)

    tg = _Bot()
    asyncio.run(ow.shutdown(_Pool(), grace_s=0.0, bot=tg))

    assert not tg.sent, f"на чистой остановке владельцу что-то ушло: {tg.sent}"


def test_the_entry_point_passes_the_bot():
    """Без этой проводки слив мёртв: bot=None, и отправлять нечем."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "main.py"), encoding="utf-8") as f:
        src = f.read()
    assert "op_worker.shutdown(pool, bot=bot)" in src, (
        "main.py зовёт остановку воркера без бота — последние защитные тревоги "
        "отправлять будет нечем"
    )

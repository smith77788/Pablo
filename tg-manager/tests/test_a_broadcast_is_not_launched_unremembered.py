"""Рассылка, которую операция не запомнила, не должна уходить.

Немедленная рассылка создаётся в два шага: строка в `broadcasts`, затем её id
закрепляется в params операции. Закрепление и есть защита от второй рассылки:
любой повторный прогон (таймаут, воскрешение после рестарта, ручной повтор)
читает params, и если id там нет — заводит ВТОРУЮ строку рассылки и
отправляет её всей аудитории заново. Журнал доставок первой рассылки второй не
указ: он ведётся по её собственному id.

Результат этой записи выбрасывался на пол (`_safe_execute` отдаёт 'ERROR', и
его никто не смотрел). То есть при сбое базы рассылка уходила НЕЗАПОМНЕННОЙ, и
цена одного блипа — вся аудитория бота получает сообщение дважды.

Теперь закрепление — цена запуска: повтор, а при полной неудаче рассылка не
стартует и снимается (отправлять она ещё не начинала). Не отправить дешевле,
чем отправить дважды — то же правило, что у инвайта, Growth Agent и кампании
ЛС, см. CLAUDE.md.
"""
from __future__ import annotations

import asyncio
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _total(name: str) -> float:
    from services import metrics

    counters = (metrics.snapshot() or {}).get("counters") or {}
    return float(sum(v for k, v in counters.items()
                     if k == name or k.startswith(name + "{")))


class _Pool:
    """Стенд базы: по требованию роняет ИМЕННО закрепление id в операции."""

    def __init__(self, fail_pin: bool = False):
        self.fail_pin = fail_pin
        self.pins = 0

    async def execute(self, sql, *args):
        if "params = COALESCE(params" in sql:
            self.pins += 1
            if self.fail_pin:
                raise RuntimeError("стенд: закрепление отклонено")
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        if "FROM managed_bots" in sql:
            return {"token": "123456789:AAH-stand", "bot_id": 777}
        return None

    async def fetch(self, sql, *args):
        if "FROM bot_users" in sql:
            return [{"user_id": 1001}, {"user_id": 1002}]
        return []

    async def fetchval(self, sql, *args):
        return None


class _Bot:
    pass


@pytest.fixture
def stand(monkeypatch):
    """Подменяем всё, что исполнитель зовёт вокруг закрепления."""
    from services import broadcaster, op_worker
    from database import db

    started: list[int] = []
    abandoned: list[int] = []

    async def _create(pool, bot_id, text, total, owner_id, **kw):
        return 4242

    def _start(*a, **kw):
        started.append(int(a[2]))

    async def _abandon(pool, broadcast_id):
        abandoned.append(int(broadcast_id))

    async def _await_bc(*a, **kw):
        return {"status": "done", "summary": "стенд: дождались"}

    async def _instant(_s):
        return None

    monkeypatch.setattr(db, "create_broadcast", _create)
    monkeypatch.setattr(broadcaster, "start", _start)
    monkeypatch.setattr(broadcaster, "abandon_unstarted", _abandon)
    monkeypatch.setattr(broadcaster, "is_running", lambda _i: False)
    monkeypatch.setattr(op_worker, "_await_broadcast", _await_bc)
    monkeypatch.setattr(asyncio, "sleep", _instant)
    return {"started": started, "abandoned": abandoned}


def test_an_unremembered_broadcast_is_not_sent(stand):
    from services import op_worker

    before = _total("infragram_broadcast_pin_write_failures_total")
    pool = _Pool(fail_pin=True)
    res = _run(op_worker._exec_run_broadcast(
        pool, _Bot(), 900, 55, {"bot_id": 777, "text": "Привет"}))

    assert stand["started"] == [], (
        "рассылка ушла, не будучи закреплённой за операцией: повторный прогон "
        "заведёт вторую и отправит её всей аудитории заново")
    assert res["status"] == "failed", res
    assert pool.pins == 2, (
        f"попыток закрепления {pool.pins} — блип базы должен переживаться "
        "повтором, а не отказом с первого раза")
    assert stand["abandoned"] == [4242], (
        "строка рассылки осталась в 'pending': старт процесса поднимет её и "
        "отправит мимо операции")
    assert _total("infragram_broadcast_pin_write_failures_total") > before


def test_a_remembered_broadcast_goes_out(stand):
    """Проверка выше была бы вечно зелёной, если рассылка не идёт никогда."""
    from services import op_worker

    pool = _Pool(fail_pin=False)
    res = _run(op_worker._exec_run_broadcast(
        pool, _Bot(), 901, 55, {"bot_id": 777, "text": "Привет"}))

    assert stand["started"] == [4242], stand
    assert stand["abandoned"] == [], "здоровую рассылку сняли"
    assert pool.pins == 1, "здоровое закрепление не должно повторяться"
    assert res["status"] == "done", res

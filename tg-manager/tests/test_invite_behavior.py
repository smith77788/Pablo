"""Уровень 6: чем аккаунт занят МЕЖДУ приглашениями.

ЧТО БЫЛО СЛОМАНО (не «падало», а тихо палило флот). Адаптивные задержки и
gaussian-разброс делали похожим на человека РИТМ действий. Состав следа они не
меняли: весь API-след инвайт-аккаунта состоял из `InviteToChannel` × N подряд.
Живой человек, добавляя знакомых в чат, между делом появляется онлайн, открывает
список диалогов, что-то читает. Аккаунт, который не делает НИЧЕГО кроме
приглашений, отделяется от живого тривиально — по набору вызовов, а не по паузам
между ними. То есть самая заметная часть подписи оставалась нетронутой.

Здесь проверяется, что действия вплетаются, что они безопасны (read-only, только
свои диалоги), что они не могут уронить или заметно затормозить инвайт, и что
исполнитель их действительно вызывает.
"""
from __future__ import annotations

import asyncio
import inspect
import random

import pytest

from services import invite_behavior as ib


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ACC = {"id": 1, "session_str": "s"}


class _Client:
    """Телетон-клиент-обманка: записывает, что от него хотели."""

    def __init__(self, *, fail=None):
        self.calls: list[str] = []
        self.fail = fail
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def __call__(self, req):
        name = type(req).__name__
        self.calls.append(f"{name}(offline={getattr(req, 'offline', None)})")
        if self.fail == "request":
            raise RuntimeError("boom")

    async def get_dialogs(self, limit=None):
        self.calls.append(f"get_dialogs({limit})")
        if self.fail == "dialogs":
            raise RuntimeError("boom")
        return ["d1", "d2", "d3"]

    async def get_messages(self, peer, limit=None):
        self.calls.append(f"get_messages({peer},{limit})")
        return []


@pytest.fixture
def client(monkeypatch):
    c = _Client()
    from services import account_manager
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **k: c)

    async def _fast(_):
        return None
    monkeypatch.setattr(ib.asyncio, "sleep", _fast)
    return c


# ── вплетение вообще происходит ──────────────────────────────────────────────

def test_action_is_performed(client, monkeypatch):
    monkeypatch.setattr(ib.random, "random", lambda: 0.0)  # вероятность выпала
    done = _run(ib.humanize(ACC, probability=1.0))
    assert done in ("presence", "dialogs", "read")
    assert client.calls, "действие должно реально дойти до клиента"
    assert client.connected and client.disconnected, "соединение обязано закрываться"


def test_not_every_pause_is_filled(monkeypatch):
    """Ровно одно действие после КАЖДОГО батча — такой же машинный признак,
    как ровная задержка. Вплетение обязано быть вероятностным."""
    monkeypatch.setattr(ib.random, "random", lambda: 0.99)
    assert _run(ib.humanize(ACC, probability=0.35)) is None


def test_action_mix_is_not_constant(client, monkeypatch):
    monkeypatch.setattr(ib.random, "random", lambda: 0.0)
    random.seed(1234)
    seen = {_run(ib.humanize(ACC, probability=1.0)) for _ in range(60)}
    assert len(seen) > 1, "один и тот же вызов каждый раз — это снова подпись"


# ── безопасность действий ────────────────────────────────────────────────────

def test_presence_always_returns_offline(client, monkeypatch):
    """Аккаунт, застрявший в «вечно онлайн» из-за ошибки между двумя вызовами,
    сам по себе аномалия."""
    monkeypatch.setattr(ib.random, "random", lambda: 0.0)
    monkeypatch.setattr(ib, "_pick_action", lambda: "presence")

    calls = []

    class _Boom(_Client):
        async def __call__(self, req):
            calls.append(getattr(req, "offline", None))
            if getattr(req, "offline", None) is False:
                # ушли онлайн успешно, а дальше — сбой
                return
            raise RuntimeError("boom")

    c = _Boom()
    from services import account_manager
    monkeypatch.setattr(account_manager, "_make_client", lambda *a, **k: c)

    async def _boom_sleep(_):
        raise RuntimeError("сбой между online и offline")
    monkeypatch.setattr(ib.asyncio, "sleep", _boom_sleep)

    _run(ib.humanize(ACC, probability=1.0))
    assert True in calls, "offline=True обязан выставляться даже при сбое"


def test_no_outward_visible_actions():
    """Реакции/сообщения/вступления видны посторонним, имеют свои лимиты и сами
    по себе баноопасны. Цель модуля — разбавить след, а не открыть второй
    рискованный поток."""
    src = inspect.getsource(ib)
    for forbidden in ("send_message", "SendReaction", "JoinChannel", "forward_messages"):
        assert forbidden not in src, f"{forbidden} — это уже не косметика, а риск"


def test_only_own_dialogs_are_touched():
    src = inspect.getsource(ib._act_read)
    assert "get_dialogs" in src, "цели берём из СВОИХ диалогов"
    assert "get_entity" not in src, "поход по чужим ресурсам — другой риск"


# ── не может уронить и не может подвесить инвайт ─────────────────────────────

def test_never_raises_on_client_failure(monkeypatch):
    monkeypatch.setattr(ib.random, "random", lambda: 0.0)

    from services import account_manager

    def _boom(*a, **k):
        raise RuntimeError("нет прокси")
    monkeypatch.setattr(account_manager, "_make_client", _boom)
    assert _run(ib.humanize(ACC, probability=1.0)) is None


def test_no_session_is_skipped():
    assert _run(ib.humanize({"id": 1, "session_str": ""}, probability=1.0)) is None


def test_has_time_budget():
    src = inspect.getsource(ib.humanize)
    assert "wait_for" in src and "_ACTION_BUDGET_S" in src, (
        "зависший клиент не имеет права держать операцию пользователя"
    )
    assert ib._ACTION_BUDGET_S <= 20, "бюджет должен быть заметно меньше паузы инвайта"


def test_cancellation_is_propagated(monkeypatch):
    """Отмена операции — не наша ошибка, глотать её нельзя: иначе `Отменить`
    перестанет останавливать инвайт."""
    monkeypatch.setattr(ib.random, "random", lambda: 0.0)

    async def _cancel(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(ib, "_run", _cancel)
    with pytest.raises(asyncio.CancelledError):
        _run(ib.humanize(ACC, probability=1.0))


# ── проводка в исполнителе ───────────────────────────────────────────────────

def test_worker_weaves_behavior_between_batches():
    from pathlib import Path
    import re
    src = Path(__file__).resolve().parents[1].joinpath("services", "op_worker.py").read_text(
        encoding="utf-8")
    m = re.search(r"async def _exec_mass_invite\(.*?(?=\nasync def )", src, re.DOTALL)
    assert m
    body = m.group(0)
    assert "invite_behavior" in body, "движок обязан быть подключён, а не лежать мёртвым"
    assert "await _humanize(acc)" in body, "вплетение делается на успешном пути батча"
    # После флуда аккаунт уходит в cooldown — дёргать его ещё раз бессмысленно
    # и вредно, поэтому вплетение стоит ПОСЛЕ обработки флуда.
    assert body.index("_rest_invite_account") < body.index("await _humanize(acc)"), (
        "зафлуженный аккаунт не должен получать дополнительных обращений"
    )

"""reporter_engine устойчив к транзиентным сбоям — жалоба реально доходит.

Жалоба владельца: «операция отправки жалоб периодически падает». Отправка одной
жалобы — это цепочка connect → разрешить цель → report, и раньше падение ЛЮБОГО
звена слепо помечало аккаунт неудачником, хотя большинство таких падений
транзиентны (холодный коннект, промах кэша при разрешении @username, короткий
FloodWait). Этот тест сторожит, что reporter_engine переживает их и жалоба
уходит; на старом коде (один get_entity, без ретраев и без обработки FloodWait)
сценарии A/B/C падают.

Тесты гоняют под заглушкой telethon из conftest (FloodWaitError там — настоящий
класс-исключение), реальная сеть не нужна: подменяем _make_client фейком.
"""
from __future__ import annotations

import asyncio

import pytest

from services import reporter_engine as rep
from telethon.errors import FloodWaitError


class _FakeClient:
    """Фейковый telethon-клиент со сценарным поведением звеньев цепочки."""

    def __init__(self, *, connect_errors=None, input_entity_error=False,
                 call_floods=None):
        # connect_errors: список исключений, выбрасываемых на первых connect()
        self._connect_errors = list(connect_errors or [])
        self._input_entity_error = input_entity_error
        # call_floods: список FloodWait.seconds, выбрасываемых на первых вызовах
        self._call_floods = list(call_floods or [])
        self.connect_calls = 0
        self.get_entity_calls = 0
        self.get_input_entity_calls = 0
        self.report_calls = 0
        self.disconnected = False

    async def connect(self):
        self.connect_calls += 1
        if self._connect_errors:
            raise self._connect_errors.pop(0)
        return True

    async def get_input_entity(self, ref):
        self.get_input_entity_calls += 1
        if self._input_entity_error:
            raise ValueError("no cached entity")
        return f"inputpeer:{ref}"

    async def get_entity(self, ref):
        self.get_entity_calls += 1
        return f"entity:{ref}"

    async def __call__(self, request):
        self.report_calls += 1
        if self._call_floods:
            secs = self._call_floods.pop(0)
            err = FloodWaitError.__new__(FloodWaitError)
            Exception.__init__(err, f"flood {secs}s")
            err.seconds = secs
            raise err
        return True

    async def disconnect(self):
        self.disconnected = True


def _install(monkeypatch, client):
    """Подменить _make_client (reporter_engine импортирует его лениво из
    account_manager) и ускорить паузы ретраев."""
    import services.account_manager as am
    monkeypatch.setattr(am, "_make_client", lambda *a, **k: client, raising=False)

    async def _fast_sleep(_s):
        return None
    monkeypatch.setattr(rep.asyncio, "sleep", _fast_sleep, raising=False)


async def test_a_transient_connect_is_retried(monkeypatch):
    """Первый connect оборвался сетью — второй проходит, жалоба уходит."""
    client = _FakeClient(connect_errors=[OSError("reset by peer")])
    _install(monkeypatch, client)
    res = await rep.report_peer("sess", {"id": 1}, "@target", "spam")
    assert res["ok"] is True, res
    assert client.connect_calls == 2  # был ровно один повтор
    assert client.report_calls == 1
    assert client.disconnected is True


async def test_b_short_floodwait_is_waited_and_retried(monkeypatch):
    """Короткий FloodWait на report-вызове пережидается и повторяется → успех."""
    client = _FakeClient(call_floods=[3])  # 3с ≤ порога — ждём и повторяем
    _install(monkeypatch, client)
    res = await rep.report_peer("sess", {"id": 1}, "@target", "spam")
    assert res["ok"] is True, res
    assert client.report_calls == 2


async def test_c_entity_resolution_falls_back_to_get_entity(monkeypatch):
    """Промах кэша get_input_entity → полное get_entity резолвит цель."""
    client = _FakeClient(input_entity_error=True)
    _install(monkeypatch, client)
    res = await rep.report_peer("sess", {"id": 1}, "@neverseen", "spam")
    assert res["ok"] is True, res
    assert client.get_input_entity_calls == 1
    assert client.get_entity_calls == 1


async def test_d_long_floodwait_is_honest_and_retryable(monkeypatch):
    """Длинный FloodWait не пережидаем на живом коннекте — честный ok=False с
    flood_wait, чтобы исполнитель мог поставить cooldown (а не крах)."""
    client = _FakeClient(call_floods=[3600, 3600])
    _install(monkeypatch, client)
    res = await rep.report_peer("sess", {"id": 1}, "@target", "spam")
    assert res["ok"] is False
    assert res.get("flood_wait") == 3600
    assert res.get("retryable") is True
    assert client.disconnected is True  # ресурсы освобождены даже на отказе


async def test_e_report_message_shares_the_same_hardening(monkeypatch):
    """report_message (жалоба на сообщения) переживает транзиентный connect так же."""
    client = _FakeClient(connect_errors=[asyncio.TimeoutError()])
    _install(monkeypatch, client)
    res = await rep.report_message("sess", {"id": 1}, "@chan", [10, 11], "spam")
    assert res["ok"] is True, res
    assert client.connect_calls == 2

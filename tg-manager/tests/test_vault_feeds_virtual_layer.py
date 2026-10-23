"""Любой ответ клиента — уже сигнал слою, а не только совпадение с правилом.

ЧТО БЫЛО. Сенсор намерений подавал сигнал только когда сработало правило-фраза
И сменилась стадия. Значит человек мог месяцами вести живую переписку, но если
он не произносил слов «сколько стоит» или «как оплатить», слой не получал о нём
НИЧЕГО. Состояние не подтверждалось и остужалось распадом — модель считала
активного клиента остывшим.

ЧТО ТЕПЕРЬ. Входящее сообщение подаёт сигнал «ответил» до разбора правил.
Сигнал не опускает состояние, только подтверждает, — значит квалифицированному
клиенту он не вредит, а просто отодвигает распад.

Тот же путь используют оба источника входящих: Хранилище (бизнес-бот) и
слушатель аккаунтов — поэтому проверка на одном покрывает оба.
"""
from __future__ import annotations

import inspect

import pytest

from services import intent_sensor


class _Pool:
    def __init__(self, rules=()):
        self.rules = list(rules)

    async def fetch(self, sql, *args):
        return self.rules

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        return None

    async def execute(self, sql, *args):
        return None

    async def executemany(self, sql, rows):
        return None


_PEER = {"peer_user_id": 777, "peer_name": "Аня", "peer_username": "anya"}


async def test_plain_reply_signals_the_layer(monkeypatch):
    seen = {}

    async def _sig(pool, owner_id, tg_user_id, name, **kw):
        seen.update(owner=owner_id, tg=tg_user_id, name=name, kw=kw)

    from services import virtual_layer
    monkeypatch.setattr(virtual_layer, "signal_for_telegram_user", _sig)

    # Правил нет вообще — прежний код вышел бы сразу и не сказал слою ничего.
    out = await intent_sensor.scan_incoming(_Pool(), None, 7, _PEER, "привет!")
    assert out == {"matched": 0}
    assert seen.get("name") == "replied", seen
    assert seen.get("tg") == 777 and seen.get("owner") == 7
    assert seen["kw"]["source"] == "vault"


async def test_signal_goes_before_the_rules_are_read(monkeypatch):
    """Иначе ранний выход «совпадений нет» снова проглотит сигнал."""
    src = inspect.getsource(intent_sensor.scan_incoming)
    i_sig = src.index("signal_for_telegram_user")
    i_rules = src.index("FROM vault_intent_rules")
    assert i_sig < i_rules, "сигнал снова зависит от того, сработало ли правило"


async def test_empty_text_signals_nothing(monkeypatch):
    called = False

    async def _sig(*a, **k):
        nonlocal called
        called = True

    from services import virtual_layer
    monkeypatch.setattr(virtual_layer, "signal_for_telegram_user", _sig)
    await intent_sensor.scan_incoming(_Pool(), None, 7, _PEER, "")
    assert not called, "пустое сообщение — не ответ"


async def test_layer_failure_does_not_break_the_sensor(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("слой лежит")

    from services import virtual_layer
    monkeypatch.setattr(virtual_layer, "signal_for_telegram_user", _boom)
    out = await intent_sensor.scan_incoming(_Pool(), None, 7, _PEER, "привет!")
    assert out == {"matched": 0}, "сбой слоя уронил обработку сообщения"


def test_both_incoming_paths_go_through_this_function():
    """Хранилище и слушатель аккаунтов — один вход, значит одно место правки."""
    for path in ("bot/handlers/business_vault.py",
                 "services/audience_listener.py"):
        src = open(path, encoding="utf-8").read()
        assert "scan_incoming(" in src, path


def test_signal_is_known_and_does_not_demote():
    from services import virtual_layer as vl
    assert vl.SIGNAL_TARGET["replied"] == "interested"
    # Уже квалифицированному «ответил» не опускает планку.
    assert vl.apply_signal({"value": "qualified", "confidence": 0.8},
                           "replied") is None or vl.rank(
        vl.apply_signal({"value": "qualified", "confidence": 0.8},
                        "replied")["value"]) >= vl.rank("qualified")

"""Рабочее окно авто-ответа считается по часам ВЛАДЕЛЬЦА, а не по UTC.

Правило «отвечать с 9 до 21» сравнивалось с `datetime.utcnow().hour`. Часы
владелец ставит по своему времени — в мини-аппе так и подписано, «🕐 9–21h», —
а пояс по умолчанию у всего продукта Europe/Moscow (services/quiet_hours.py,
services/recipient_time.py). То есть правило молчало с 9 до 12 по Москве,
когда клиенты как раз пишут, и отвечало с 21 до 24, когда владелец спит.
Ошибка ровно на разницу поясов, и видел её КЛИЕНТ, а не лог: на его сообщение
никто не отвечал.

Пояс читается оттуда же, откуда тихие часы — `notification_settings.tz`, —
чтобы договорённость о времени владельца у продукта была одна.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
from unittest.mock import AsyncMock

import pytest

from services import auto_responder as ar

ROOT = pathlib.Path(__file__).resolve().parents[1]
BOT_ID = 555
TOKEN = "123456789:AAHrealtokenlooking-value_0123456789ab"
OWNER = 4242


class _Pool:
    """Пул, который помнит, о чём его спрашивали."""

    def __init__(self, tz="Europe/Moscow"):
        self.tz = tz
        self.tz_asked = 0

    async def fetchrow(self, query, *args):
        if "notification_settings" in query:
            self.tz_asked += 1
            return {"tz": self.tz}
        if "FROM managed_bots" in query:
            return {"bot_role": None, "swarm_enabled": False, "cluster": None,
                    "added_by": OWNER, "username": "b", "first_name": "b",
                    "relay_enabled": False}
        return None

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        return "UPDATE 1"


def _clock(utc_hour: int, utc_minute: int = 30):
    """Подменённый `datetime` модуля: «сейчас» задано в UTC."""
    base = dt.datetime(2026, 11, 5, utc_hour, utc_minute, tzinfo=dt.timezone.utc)

    class _C(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    return _C


def test_the_owner_hour_is_the_owners_zone(monkeypatch):
    """07:30 UTC для Москвы — это 10 часов, а не 7."""
    monkeypatch.setattr(ar, "datetime", _clock(7))
    pool = _Pool()
    assert asyncio.run(ar._owner_hour(pool, OWNER)) == 10
    assert pool.tz_asked == 1


def test_a_missing_timezone_falls_back_to_moscow(monkeypatch):
    """Нет строки в настройках — пояс как у всего продукта."""
    monkeypatch.setattr(ar, "datetime", _clock(7))
    assert asyncio.run(ar._owner_hour(_Pool(tz=None), OWNER)) == 10
    assert asyncio.run(ar._owner_hour(_Pool(tz="  "), OWNER)) == 10
    assert asyncio.run(ar._owner_hour(_Pool(tz="Марс/Олимп"), OWNER)) == 10


def test_another_zone_is_respected(monkeypatch):
    """Владелец во Владивостоке получает свой час, а не московский."""
    monkeypatch.setattr(ar, "datetime", _clock(7))
    assert asyncio.run(ar._owner_hour(_Pool(tz="Asia/Vladivostok"), OWNER)) == 17


def test_a_broken_database_does_not_break_the_window(monkeypatch):
    """Моргнула база — отвечаем по Москве, а не падаем посреди разбора."""
    class _Bad(_Pool):
        async def fetchrow(self, query, *args):
            raise RuntimeError("база моргнула")

    monkeypatch.setattr(ar, "datetime", _clock(7))
    assert asyncio.run(ar._owner_hour(_Bad(), OWNER)) == 10


def _drive(monkeypatch, rule, owner_hour):
    """Один разбор сообщения. Возвращает список отправленных ответов."""
    sent: list[str] = []

    async def _send(pool, http, token, bot_id, chat_id, r, rendered):
        sent.append(rendered)

    async def _hour(pool, owner_id):
        return owner_hour

    async def _call(session, token, method, **params):
        return {"ok": True, "result": {}}

    monkeypatch.setattr(ar, "_send_rule_reply", _send)
    monkeypatch.setattr(ar, "_owner_hour", _hour)
    monkeypatch.setattr(ar.bot_api, "_call", _call)
    monkeypatch.setattr("database.db.get_active_auto_replies", AsyncMock(return_value=[rule]))
    monkeypatch.setattr("database.db.get_active_funnels", AsyncMock(return_value=[]))
    monkeypatch.setattr("database.db.get_active_automation_rules", AsyncMock(return_value=[]))
    monkeypatch.setattr("database.db.get_active_experiment", AsyncMock(return_value=None))
    monkeypatch.setattr("database.db.upsert_users", AsyncMock(return_value=[]))
    monkeypatch.setattr("database.db.get_update_offset", AsyncMock(return_value=100))
    monkeypatch.setattr("database.db.set_update_offset", AsyncMock())
    monkeypatch.setattr("services.brand_injection.is_free_tier", AsyncMock(return_value=False))
    monkeypatch.setattr(ar, "_record_bot_ok", AsyncMock())
    monkeypatch.setattr(ar, "_record_bot_error", AsyncMock())
    upd = {"update_id": 101, "message": {
        "message_id": 1, "from": {"id": 7001, "is_bot": False, "first_name": "Артур"},
        "chat": {"id": 7001, "type": "private"}, "text": "цена"}}
    asyncio.run(ar._process_bot(_Pool(), object(), BOT_ID, TOKEN, None, updates=[upd]))
    return sent


_RULE = {"trigger_type": "keyword", "keyword": "цена", "match_mode": "contains",
         "response_text": "Прайс вот такой", "reply_delay_sec": 0,
         "active_from_hour": 9, "active_to_hour": 21, "priority": 1}


def test_inside_the_owners_window_the_client_gets_an_answer(monkeypatch):
    """10 часов у владельца — окно 9–21 открыто, ответ уходит."""
    assert _drive(monkeypatch, dict(_RULE), owner_hour=10) == ["Прайс вот такой"]


def test_outside_the_owners_window_the_rule_stays_silent(monkeypatch):
    """7 часов у владельца — окно закрыто, и это уже не про UTC."""
    assert _drive(monkeypatch, dict(_RULE), owner_hour=7) == []


def test_a_rule_without_a_window_never_asks_the_zone(monkeypatch):
    """Окна нет — лишнего запроса в базу тоже нет."""
    asked = {"n": 0}

    async def _hour(pool, owner_id):
        asked["n"] += 1
        return 3

    rule = dict(_RULE, active_from_hour=None, active_to_hour=None)
    monkeypatch.setattr(ar, "_owner_hour", _hour)
    sent = _drive(monkeypatch, rule, owner_hour=3)
    assert sent == ["Прайс вот такой"], sent
    assert asked["n"] == 0, "пояс владельца спросили у правила без окна"


def test_the_window_no_longer_reads_the_utc_hour():
    """Ратчет: в разборе правил нет наивного UTC-часа.

    `datetime.utcnow()` здесь не просто устаревший вызов — это другой часовой
    пояс, то есть другое поведение продукта.

    Оба утверждения — по дереву. Первое потому, что в докстрингах этот вызов
    УПОМИНАЕТСЯ (им и объясняется, что было), и проверка по подстроке ловила бы
    объяснение. Второе потому, что окно фиксированной длины вокруг найденного
    комментария промахивается от любой правки выше по файлу, и «искомого нет»
    становится правдой само собой — защита выключается молча. Границы берём у
    функции.
    """
    import ast

    src = (ROOT / "services" / "auto_responder.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    bad = [n for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "utcnow"]
    assert not bad, (
        f"в автоответчике снова появилось наивное время, строки: "
        f"{[n.lineno for n in bad]}")

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_process_bot"), None)
    assert fn is not None, "в автоответчике не стало _process_bot"
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_owner_hour" in called, (
        "разбор правил больше не спрашивает час владельца")
    assert "_within_active_window" in called, (
        "рабочее окно правила больше не проверяется")

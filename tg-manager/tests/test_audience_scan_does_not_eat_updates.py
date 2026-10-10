"""Сбор аудитории не съедает входящие сообщения у автоответчика.

ЧТО БЫЛО. `scan_all_users` листал обновления пачками, и каждая следующая
просьба шла с `offset = последний + 1`. Для Telegram это ПОДТВЕРЖДЕНИЕ: всё,
что раньше этого номера, он забывает и больше не отдаёт никому. Очередь
обновлений у бота одна на всех, а разбирает её автоответчик.

Значит нажатие «Сканировать все апдейты» забирало сообщения, которых
автоответчик ещё не видел: человек написал боту, сбор аудитории записал его в
базу — и на этом всё. Ни авто-ответа, ни запуска воронки, ни обработки
`/start`, ни пересылки оператору. Подписчик остался без ответа навсегда, а
владелец об этом не узнал: экран показывал успешный сбор.

Хуже того, обработчик потом сохранял полученный `last_id` как общий оффсет
бота — то есть автоответчик ещё и перепрыгивал через эти сообщения сам.

Листать было незачем: Telegram хранит только НЕподтверждённые обновления, а
автоответчик опрашивает бота каждые 10 секунд. «50 пачек по 100» — это в
лучшем случае одно десятисекундное окно.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from services import bot_api


def _run(coro):
    return asyncio.run(coro)


class _Recorder:
    """Подменяет _call и записывает, с какими offset просили обновления."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.offsets: list[int] = []

    async def __call__(self, session, token, method, **params):
        self.offsets.append(params.get("offset"))
        batch = self.batches.pop(0) if self.batches else []
        return {"ok": True, "result": batch}


def _upd(update_id: int, user_id: int, username: str = ""):
    return {
        "update_id": update_id,
        "message": {
            "from": {"id": user_id, "username": username or None,
                     "first_name": "Имя", "is_bot": False},
            "chat": {"id": user_id},
            "text": "привет",
        },
    }


def test_scan_never_confirms_what_it_read(monkeypatch):
    """Подтверждение = потеря сообщения для автоответчика."""
    rec = _Recorder([[_upd(900, 11), _upd(901, 12)]])
    monkeypatch.setattr(bot_api, "_call", rec)

    users, last_id = _run(bot_api.scan_all_users(None, "t0ken", start_offset=800))

    assert rec.offsets == [0], (
        f"обновления запрошены с offset={rec.offsets} — всё, кроме 0, Telegram "
        "понимает как подтверждение и забывает прочитанное"
    )
    assert [u["user_id"] for u in users] == [11, 12]
    assert last_id == 901


def test_scan_does_not_page(monkeypatch):
    """Листать нечего: Telegram держит только неподтверждённые обновления."""
    rec = _Recorder([[_upd(i, 100 + i) for i in range(100)], [_upd(999, 777)]])
    monkeypatch.setattr(bot_api, "_call", rec)

    _run(bot_api.scan_all_users(None, "t0ken"))

    assert len(rec.offsets) == 1, (
        f"сделано {len(rec.offsets)} запросов: каждый следующий подтверждает "
        "предыдущий и забирает сообщения у автоответчика"
    )


def test_bots_and_duplicates_do_not_get_into_the_audience(monkeypatch):
    batch = [
        _upd(1, 11, "arthur"),
        _upd(2, 11, "arthur"),
        {"update_id": 3, "message": {"from": {"id": 42, "is_bot": True},
                                     "chat": {"id": 42}, "text": "я бот"}},
        {"update_id": 4},
    ]
    monkeypatch.setattr(bot_api, "_call", _Recorder([batch]))

    users, last_id = _run(bot_api.scan_all_users(None, "t0ken"))

    assert [u["user_id"] for u in users] == [11]
    assert last_id == 4


def test_webhook_refusal_still_reaches_the_owner(monkeypatch):
    async def _conflict(session, token, method, **params):
        return {"ok": False, "error_code": 409,
                "description": "Conflict: can't use getUpdates method while webhook is active"}

    monkeypatch.setattr(bot_api, "_call", _conflict)
    with pytest.raises(bot_api.UpdatesUnavailable):
        _run(bot_api.scan_all_users(None, "t0ken"))


def test_the_screen_no_longer_stores_the_offset():
    """Обработчик не имеет права двигать общий оффсет бота после сбора."""
    from bot.handlers import audience

    src = inspect.getsource(audience)
    scan = src[src.index("scan_all_users"):]
    scan = scan[:scan.index("@router")] if "@router" in scan else scan
    assert "set_update_offset" not in scan, (
        "сбор аудитории снова двигает оффсет — автоответчик перепрыгнет через "
        "эти сообщения"
    )

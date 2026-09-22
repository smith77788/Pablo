"""Один сломавшийся апдейт не превращает бота в спамера на сутки.

ЧТО БЫЛО. Разбор апдейта в `_process_bot` — длинный путь: авто-ответы,
правила автоматизации, воронки, эксперименты, релей оператору. Исключение в
любом месте этого пути улетало в общий `except` внизу функции, а вместе с ним
пропадал и сдвиг оффсета: `set_update_offset` стоял ПОСЛЕ цикла.

Telegram отдаёт апдейты начиная с сохранённого оффсета. Оффсет не сдвинулся —
значит следующий цикл (через 10 секунд) приносит ТОТ ЖЕ пакет, бот падает на
том же сообщении и снова ничего не сдвигает. И так сутки, пока Telegram сам не
выкинет апдейт из очереди.

Всё это время бот каждые 10 секунд заново отвечает на уже обработанные
сообщения, заново запускает воронки и заново пересылает оператору те же
диалоги. Владелец видит бота-спамера, а в логе — одну и ту же трассировку.

ЧТО СТАЛО. Оффсет сдвигается в `finally`. `max_update_id` растёт в начале
каждой итерации, поэтому на момент падения он равен ровно тому апдейту, на
котором споткнулись: отравленный пропускается, хвост пакета возвращается
следующим циклом и разбирается нормально.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from services import auto_responder as ar

BOT_ID = 555
TOKEN = "123456789:AAHrealtokenlooking-value_0123456789ab"
START_OFFSET = 100


def _run(coro):
    return asyncio.run(coro)


def _upd(update_id: int, text: str, user_id: int = 7001) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "Артур"},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


class _FakePool:
    async def fetchrow(self, query, *args):
        if "FROM managed_bots" in query:
            return {"bot_role": None, "swarm_enabled": False, "cluster": None,
                    "added_by": 1, "username": "b", "first_name": "b",
                    "relay_enabled": False}
        return None

    async def fetch(self, query, *args):
        return []

    async def fetchval(self, query, *args):
        return None

    async def execute(self, query, *args):
        return "UPDATE 1"


def _drive(updates, poison_text=None):
    """Прогнать один цикл разбора. Возвращает сохранённый оффсет или None."""
    saved: list[int] = []

    async def _set_offset(pool, bot_id, value):
        saved.append(value)

    async def _call(session, token, method, **params):
        if method == "getUpdates":
            return {"ok": True, "result": updates}
        return {"ok": True, "result": {}}

    def _stop_word(text):
        if poison_text is not None and text == poison_text:
            raise RuntimeError("разбор апдейта сломался")
        return False

    patches = [
        patch.object(ar, "_is_stop_word", _stop_word),
        patch.object(ar.bot_api, "_call", _call),
        patch("database.db.get_update_offset", AsyncMock(return_value=START_OFFSET)),
        patch("database.db.set_update_offset", _set_offset),
        patch("database.db.get_active_auto_replies", AsyncMock(return_value=[])),
        patch("database.db.get_active_funnels", AsyncMock(return_value=[])),
        patch("database.db.get_active_automation_rules", AsyncMock(return_value=[])),
        patch("database.db.get_active_experiment", AsyncMock(return_value=None)),
        patch("database.db.upsert_users", AsyncMock(return_value=[])),
        patch.object(ar, "_record_bot_ok", AsyncMock()),
        patch.object(ar, "_record_bot_error", AsyncMock()),
        patch("services.brand_injection.is_free_tier", AsyncMock(return_value=False)),
    ]
    for p in patches:
        p.start()
    try:
        _run(ar._process_bot(_FakePool(), object(), BOT_ID, TOKEN))
    finally:
        for p in reversed(patches):
            p.stop()
    return saved[-1] if saved else None


def test_poisoned_update_is_stepped_over():
    """Иначе тот же пакет приезжает снова каждые 10 секунд — сутки подряд."""
    saved = _drive([_upd(101, "яд"), _upd(102, "обычное")], poison_text="яд")

    assert saved is not None, (
        "оффсет не сдвинут — следующий цикл принесёт тот же пакет, бот снова "
        "упадёт на том же сообщении и так до истечения суток"
    )
    assert saved == 101, (
        f"сдвинулись на {saved}: должны перешагнуть ровно отравленный апдейт, "
        "остальной хвост пакета придёт следующим циклом"
    )


def test_healthy_batch_moves_the_offset_to_the_end():
    saved = _drive([_upd(101, "привет"), _upd(102, "ещё")])
    assert saved == 102


def test_nothing_is_saved_when_there_is_nothing_to_read():
    assert _drive([]) is None


def test_offset_does_not_go_backwards():
    """Пакет целиком старее сохранённого оффсета сдвигать нечего."""
    assert _drive([_upd(50, "старое")]) is None

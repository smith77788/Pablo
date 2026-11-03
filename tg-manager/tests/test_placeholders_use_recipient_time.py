"""{{DATE}}/{{TIME}} в текстах ботов — время ПОЛУЧАТЕЛЯ, а не сервера.

ЧТО БЫЛО. Сценарии (`workflow_runtime.render`), рассылки бота
(`broadcaster._render_for_user`) и автоответы (`auto_responder._render_text`)
брали `datetime.now()` — время сервера, а сервер живёт в UTC. Человек из
Москвы в 21:30 получал «18:30», а в 01:00 по Москве — ещё и вчерашнюю дату.

И ещё одно, в рассылке: кэш имён подписчиков считался «полным набором». Если
в нём был хоть один получатель новой рассылки, остальных не догружали, и
всем, кого прошлая рассылка не задела, уходило пустое {{FIRST_NAME}}.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

# 22:30 UTC 3 ноября = 01:30 4 ноября по Москве.
_FROZEN = datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc)


@pytest.fixture()
def frozen(monkeypatch):
    from services import recipient_time

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return _FROZEN if tz is not None else _FROZEN.replace(tzinfo=None)

    monkeypatch.setattr(recipient_time, "datetime", _DT)


def test_workflow_text_uses_recipients_clock(frozen):
    from services import workflow_runtime as rt

    out = rt.render("{{DATE}} {{TIME}}", {"language_code": "ru"}, {}, {})
    assert out == "04.11.2026 01:30", out
    # Свой пояс в карточке контакта главнее языка клиента.
    out = rt.render("{{TIME}}", {"language_code": "ru"},
                    {"timezone": "Asia/Almaty"}, {})
    assert out == "03:30", out


def test_broadcast_text_uses_recipients_clock(frozen):
    from services.broadcaster import _render_for_user

    out = _render_for_user("{{DATE}} {{TIME}}",
                           {"first_name": "Аня", "language_code": "uk"})
    assert out == "04.11.2026 00:30", out


def test_auto_reply_text_uses_recipients_clock(frozen):
    from services.auto_responder import _render_text

    out = _render_text("{{DATE}} {{TIME}}", {"first_name": "Аня", "language_code": "ru"})
    assert out == "04.11.2026 01:30", out


def test_unknown_language_falls_back_to_moscow(frozen):
    from services import recipient_time

    assert recipient_time.now_for(None, "xx").strftime("%H:%M") == "01:30"
    assert recipient_time.now_for({"timezone": "Mars/Olympus"}, None).strftime(
        "%H:%M") == "01:30"


class _Pool:
    def __init__(self, users):
        self.users = users
        self.asked: list[list[int]] = []

    async def fetch(self, query, bot_id, ids):
        self.asked.append(list(ids))
        return [dict(self.users[i]) for i in ids if i in self.users]


def test_broadcast_name_cache_loads_people_the_last_broadcast_missed():
    from services import broadcaster

    broadcaster._user_map_cache.clear()
    pool = _Pool({1: {"user_id": 1, "first_name": "Аня", "fields": None},
                  2: {"user_id": 2, "first_name": "Боря", "fields": '{"timezone": "Asia/Almaty"}'}})

    async def go():
        first = await broadcaster._load_user_map(pool, 77, [1])
        second = await broadcaster._load_user_map(pool, 77, [1, 2])
        return first, second

    try:
        first, second = asyncio.run(go())
    finally:
        broadcaster._user_map_cache.clear()
    assert first[1]["first_name"] == "Аня"
    assert second.get(2, {}).get("first_name") == "Боря", (
        "вторая рассылка не догрузила подписчика, которого не было в кэше")
    assert second[2]["fields"] == {"timezone": "Asia/Almaty"}
    # Уже загруженного второй раз из базы не тянем.
    assert pool.asked == [[1], [2]]

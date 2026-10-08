"""Правка поста перед публикацией: уходит в канал ровно то, что видит владелец.

РАЗРЫВ. В режиме «присылает пост мне на одобрение» у владельца было три
действия: принять пост целиком, попросить другой, пропустить. Поправить одну
цифру, убрать лишнюю фразу или дописать контакт было нельзя — из-за одного
слова приходилось выбрасывать весь текст и надеяться, что следующий вариант
окажется лучше. На канале, где администратор пишет по два поста в день, это
значит, что владелец либо пропускает мимо себя неточности, либо гоняет
генерацию по кругу.

Теперь `publish_draft` принимает правку: публикуется и сохраняется она, а не
исходный текст, — иначе память канала и журнал говорили бы о том, чего в
канале нет.
"""
from __future__ import annotations

import asyncio

import pytest

from services import channel_admin as ca


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Pool:
    """Заглушка пула: отдаёт один черновик и запоминает, что в него записали."""

    def __init__(self, body="Исходный текст поста"):
        self.body = body
        self.claimed = False
        self.executed = []

    async def fetchrow(self, sql, *args):
        if "UPDATE va_admin_drafts SET status=" in sql and not self.claimed:
            self.claimed = True
            return {"id": 7, "channel_id": -100500, "pillar": "Кейсы",
                    "body": self.body, "is_intro": False}
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def saved_body(self):
        for sql, args in self.executed:
            if "UPDATE va_admin_drafts SET body=" in sql:
                return args[1]
        return None

    def events(self):
        return [args[3] for sql, args in self.executed if "INSERT INTO va_admin_events" in sql]


@pytest.fixture()
def published(monkeypatch):
    """Перехватываем publish(): операция и доступные аккаунты тут ни при чём."""
    seen = {}

    async def fake_publish(pool, owner_id, channel_id, text, pillar):
        seen["text"] = text
        seen["pillar"] = pillar
        return 42

    monkeypatch.setattr(ca, "publish", fake_publish)
    return seen


def test_without_an_edit_the_original_goes_out(published):
    pool = _Pool()
    _run(ca.publish_draft(pool, 1, 7))
    assert published["text"] == "Исходный текст поста"
    assert pool.saved_body() is None, "текст переписали, хотя владелец его не трогал"


def test_the_edit_is_what_reaches_the_channel(published):
    pool = _Pool()
    _run(ca.publish_draft(pool, 1, 7, body="Поправленный текст: цена 1500 ₽"))
    assert published["text"] == "Поправленный текст: цена 1500 ₽"
    assert pool.saved_body() == "Поправленный текст: цена 1500 ₽", (
        "в канал ушла правка, а в черновике остался старый текст — память канала "
        "и журнал будут говорить о том, чего в канале нет")
    assert any("поправил" in e.lower() for e in pool.events()), (
        "правка владельца не попала в журнал администратора")


def test_an_unchanged_edit_is_not_treated_as_one(published):
    """Открыл правку, ничего не изменил, нажал «Опубликовать» — это не правка."""
    pool = _Pool()
    _run(ca.publish_draft(pool, 1, 7, body="  Исходный текст поста  "))
    assert published["text"] == "Исходный текст поста"
    assert pool.saved_body() is None
    assert not any("поправил" in e.lower() for e in pool.events())


def test_empty_edit_publishes_the_original(published):
    for empty in ("", "   ", None):
        pool = _Pool()
        _run(ca.publish_draft(pool, 1, 7, body=empty))
        assert published["text"] == "Исходный текст поста", empty
        assert pool.saved_body() is None, empty


def test_edit_is_cut_to_the_telegram_limit(published):
    pool = _Pool()
    _run(ca.publish_draft(pool, 1, 7, body="я" * 5000))
    assert len(published["text"]) == ca.MAX_POST_CHARS == 4096, (
        "слишком длинная правка уйдёт в операцию и упадёт уже там, а владелец "
        "увидит отказ без объяснения")
    assert len(pool.saved_body()) == ca.MAX_POST_CHARS


def test_the_screen_sends_the_edit_and_can_cancel_it():
    """Экран: поле правки, отправка её на сервер и возврат исходного текста."""
    import os
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "mini_app", "screens", "va_admin.js"), encoding="utf-8").read()
    assert "function vaDraftEdit(" in js and "function vaDraftEditCancel(" in js
    assert "✏️ Поправить" in js, "кнопки правки нет — до неё не добраться"
    assert "body: edited" in js or "body: edited," in js or "body: edited }" in js or (
        "edited" in js and "JSON.stringify({ reason: reason || '', body: edited })" in js), (
        "правка не уходит на сервер")
    assert "box.dataset.orig" in js, (
        "исходный текст негде взять — «Отмена» не вернёт его без похода на сервер")

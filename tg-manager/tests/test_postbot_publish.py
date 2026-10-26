"""Регрессия: код @PostBot должен публиковать подготовленный inline-пост.

До фикса ``@PostBot 40599y1mvoikjid`` уходил в канал буквальным текстом, поэтому
терялись сам пост, медиа и кнопки. Тест проходит через общий production-метод,
которым пользуются единая, массовая и отложенная публикации.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from services import account_manager, postbot

from tests.miniapp_source import py_function


def _run(coro):
    return asyncio.run(coro)


class _Message:
    id = 731


class _Result:
    def __init__(self):
        self.clicked_to = None
        self.message = type("PreparedMessage", (), {"message": "Безопасный пост"})()

    async def click(self, *, entity):
        self.clicked_to = entity
        return _Message()


class _Client:
    def __init__(self, results):
        self.results = results
        self.inline_calls = []
        self.sent_text = []

    async def inline_query(self, bot, query, *, entity):
        self.inline_calls.append((bot, query, entity))
        return self.results

    async def send_message(self, peer, text, **kwargs):
        self.sent_text.append((peer, text, kwargs))
        return _Message()


def test_reference_parser_is_strict_but_accepts_supported_forms():
    assert postbot.parse_reference("@PostBot 40599y1mvoikjid").code == "40599y1mvoikjid"
    assert postbot.parse_reference("postbot: 40599y1mvoikjid").code == "40599y1mvoikjid"
    assert postbot.parse_reference("40599y1mvoikjid").code == "40599y1mvoikjid"
    assert postbot.parse_reference("4059912345678901") is None
    assert postbot.parse_reference("обычный_пост") is None
    assert postbot.parse_reference("@OtherBot 40599y1mvoikjid") is None
    assert postbot.parse_reference("Текст и @PostBot 40599y1mvoikjid") is None
    assert postbot.has_explicit_intent(" @PostBot неправильный код") is True
    assert postbot.has_explicit_intent("PostBot — хороший бот") is False


def test_common_channel_sender_clicks_postbot_inline_result(monkeypatch):
    result = _Result()
    client = _Client([result])

    async def _connected(*args, **kwargs):
        return None

    async def _disconnected(*args, **kwargs):
        return None

    monkeypatch.setattr(account_manager, "_make_client", lambda *args: client)
    monkeypatch.setattr(account_manager, "_connect_and_track", _connected)
    monkeypatch.setattr(account_manager, "safe_disconnect", _disconnected)

    response = _run(
        account_manager.post_to_channel(
            "session",
            "@target_channel",
            "@PostBot 40599y1mvoikjid",
        )
    )

    assert response == {
        "msg_id": 731,
        "resolved_access_hash": 0,
        "source": "postbot",
    }
    assert client.inline_calls == [
        ("PostBot", "40599y1mvoikjid", "@target_channel")
    ]
    assert result.clicked_to == "@target_channel"
    assert client.sent_text == [], "код PostBot нельзя отправлять буквальным текстом"


def test_missing_postbot_result_is_a_clear_error(monkeypatch):
    client = _Client([])

    async def _connected(*args, **kwargs):
        return None

    async def _disconnected(*args, **kwargs):
        return None

    monkeypatch.setattr(account_manager, "_make_client", lambda *args: client)
    monkeypatch.setattr(account_manager, "_connect_and_track", _connected)
    monkeypatch.setattr(account_manager, "safe_disconnect", _disconnected)

    response = _run(
        account_manager.post_to_channel(
            "session",
            "@target_channel",
            "@PostBot 40599y1mvoikjid",
        )
    )

    assert response["postbot_error"] is True
    assert "не нашёл публикацию" in response["error"]
    assert client.sent_text == []


def test_postbot_reference_rejects_a_second_attachment():
    response = _run(
        account_manager.post_to_channel(
            "session",
            "@target_channel",
            "@PostBot 40599y1mvoikjid",
            media_bytes=b"not-used",
            media_type="photo",
        )
    )

    assert response["validation_error"] is True
    assert "собственные вложения" in response["error"]


def test_broken_explicit_reference_is_not_published_as_text():
    response = _run(
        account_manager.post_to_channel(
            "session",
            "@target_channel",
            "@PostBot два кода",
        )
    )

    assert response["validation_error"] is True
    assert "Неверный формат" in response["error"]


def test_expired_inline_result_is_queried_again():
    expired = type("InlineResultExpiredError", (Exception,), {})

    class _ExpiringResult(_Result):
        async def click(self, *, entity):
            if self.clicked_to is None:
                self.clicked_to = "first-attempt-expired"
                raise expired()
            self.clicked_to = entity
            return _Message()

    result = _ExpiringResult()
    client = _Client([result])

    message = _run(
        postbot.publish(
            client,
            "@target_channel",
            postbot.PostBotReference("40599y1mvoikjid"),
            timeout=1,
        )
    )

    assert message.id == 731
    assert len(client.inline_calls) == 2
    assert result.clicked_to == "@target_channel"


def test_prepared_text_cannot_bypass_content_safety(monkeypatch):
    from services import content_safety

    result = _Result()
    client = _Client([result])
    monkeypatch.setattr(
        content_safety,
        "scan_text",
        lambda text: SimpleNamespace(blocked=True),
    )

    with pytest.raises(postbot.PostBotPublishError, match="проверкой безопасности"):
        _run(
            postbot.publish(
                client,
                "@target_channel",
                postbot.PostBotReference("40599y1mvoikjid"),
                timeout=1,
            )
        )

    assert result.clicked_to is None


def test_mass_publish_keeps_postbot_reference_atomic():
    body = py_function("services/op_worker.py", "_exec_mass_publish")

    assert "postbot_reference = postbot.parse_reference(mp_text)" in body
    assert "not postbot_reference and await _bi.is_user_free_tier" in body
    assert "False if postbot_reference else _link_vary.contains_channel_link" in body
    assert "mp_text if postbot_reference else _expand_spintax(mp_text)" in body


def test_all_publish_inputs_expose_and_validate_postbot_format():
    api = py_function("services/mini_app_api.py", "mass_publish")
    assert "has_explicit_intent(text)" in api
    assert "parse_reference(text)" in api

    root = Path(__file__).resolve().parents[1]
    html = (root / "mini_app" / "index.html").read_text(encoding="utf-8")
    bot_handler = (root / "bot" / "handlers" / "mass_publish.py").read_text(
        encoding="utf-8"
    )
    assert "готовый пост: @PostBot код" in html
    assert "@PostBot код" in bot_handler

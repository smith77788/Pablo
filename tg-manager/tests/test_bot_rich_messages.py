"""Bot API 10.5 structured quote migration and actual send/edit paths."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from services import bot_api
from services.bot_rich_messages import normalize_rich_message


def test_legacy_quote_preserves_rich_text_credit_and_nested_content():
    quote = {
        "type": "expandable_blockquote",
        "text": [{"type": "bold", "text": "Привет"}],
        "credit": {"type": "url", "text": "Источник", "url": "https://example.com"},
    }
    payload = {"blocks": [{"type": "details", "blocks": [quote]}], "is_rtl": False}
    result = normalize_rich_message(payload)
    migrated = result["blocks"][0]["blocks"][0]
    assert migrated["blocks"] == [{"type": "paragraph", "text": quote["text"]}]
    assert migrated["credit"] == quote["credit"]
    assert "text" not in migrated and "text" in quote
    migrated["credit"]["text"] = "Изменено"
    assert quote["credit"]["text"] == "Источник"
    assert result["is_rtl"] is False


def test_new_quote_arbitrary_blocks_are_not_flattened():
    payload = {
        "blocks": [
            {
                "type": "expandable_blockquote",
                "blocks": [
                    {"type": "paragraph", "text": "Текст"},
                    {"type": "document", "document": "file-id", "caption": "Файл"},
                ],
                "credit": "Автор",
            }
        ],
        "skip_entity_detection": True,
    }
    assert normalize_rich_message(payload) == payload


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"html": "x", "blocks": []},
        {"markdown": ""},
        {"blocks": "wrong"},
        {"html": "x", "is_rtl": "false"},
        {"blocks": [{"type": "expandable_blockquote", "text": "x", "blocks": []}]},
        {"blocks": [{"type": "expandable_blockquote"}]},
        {"blocks": [{"type": "paragraph", "text": "x"}] * 501},
    ],
)
def test_invalid_payload_rejected_before_network(payload):
    with pytest.raises(ValueError):
        normalize_rich_message(payload)


def test_cyclic_and_excessively_nested_payload_rejected():
    cycle = {"blocks": []}
    cycle["blocks"].append(cycle)
    with pytest.raises(ValueError, match="циклическая"):
        normalize_rich_message(cycle)
    block = {"type": "paragraph", "text": "x"}
    for _ in range(18):
        block = {"type": "expandable_blockquote", "blocks": [block]}
    with pytest.raises(ValueError, match="вложенность"):
        normalize_rich_message({"blocks": [block]})


def test_send_message_routes_rich_payload_and_preserves_controls(monkeypatch):
    call = AsyncMock(return_value={"ok": True, "result": {"message_id": 7}})
    monkeypatch.setattr(bot_api, "_call", call)
    result = asyncio.run(
        bot_api.send_message(
            object(),
            "secret",
            42,
            "",
            rich_message={"html": "<h1>Заголовок</h1>"},
            disable_notification=True,
            reply_markup={"inline_keyboard": []},
        )
    )
    assert result == (True, None)
    assert call.await_args.args[2] == "sendRichMessage"
    params = call.await_args.kwargs
    assert params["rich_message"] == {"html": "<h1>Заголовок</h1>"}
    assert params["disable_notification"] is True
    assert params["reply_markup"] == {"inline_keyboard": []}
    assert "text" not in params and "parse_mode" not in params


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False, "error_code": 429, "parameters": {"retry_after": 90}},
        {"ok": False, "error_code": 500},
        {"ok": False, "error_code": 0},
        {"ok": False, "error_code": 400, "description": "Bad message"},
    ],
)
def test_send_never_falls_back_on_uncertain_or_rate_limited_results(
    monkeypatch, response
):
    call = AsyncMock(return_value=response)
    monkeypatch.setattr(bot_api, "_call", call)
    result = asyncio.run(
        bot_api.send_rich_message(
            object(),
            "secret",
            {"markdown": "**Текст**"},
            chat_id=42,
            fallback_text="Текст",
        )
    )
    assert result == response
    assert call.await_count == 1


def test_explicit_plain_fallback_only_for_unknown_method(monkeypatch):
    call = AsyncMock(
        side_effect=[
            {"ok": False, "error_code": 404, "description": "Not Found"},
            {"ok": True, "result": {"message_id": 8}},
        ]
    )
    monkeypatch.setattr(bot_api, "_call", call)
    result = asyncio.run(
        bot_api.send_rich_message(
            object(),
            "secret",
            {"html": "<h1>Текст</h1>"},
            chat_id=42,
            fallback_text="Текст",
            protect_content=True,
        )
    )
    assert result["rich_fallback"] is True
    assert call.await_args.args[2] == "sendMessage"
    assert call.await_args.kwargs == {
        "chat_id": 42,
        "text": "Текст",
        "protect_content": True,
    }


def test_inline_edit_uses_inline_id_without_fake_chat(monkeypatch):
    call = AsyncMock(return_value={"ok": True, "result": True})
    monkeypatch.setattr(bot_api, "_call", call)
    result = asyncio.run(
        bot_api.edit_rich_message(
            object(),
            "secret",
            {"html": "<p>Текст</p>"},
            inline_message_id="opaque-inline-id",
        )
    )
    assert result["ok"] is True
    assert call.await_args.args[2] == "editMessageText"
    assert call.await_args.kwargs == {
        "inline_message_id": "opaque-inline-id",
        "rich_message": {"html": "<p>Текст</p>"},
    }
    with pytest.raises(ValueError, match="один адрес"):
        asyncio.run(
            bot_api.edit_rich_message(
                object(),
                "secret",
                {"html": "x"},
                inline_message_id="inline",
                chat_id=42,
                message_id=1,
            )
        )


def test_ephemeral_send_can_omit_chat(monkeypatch):
    call = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(bot_api, "_call", call)
    asyncio.run(
        bot_api.send_rich_message(
            object(),
            "secret",
            {"html": "x"},
            ephemeral_message_parameters={"callback_query_id": "q"},
        )
    )
    assert "chat_id" not in call.await_args.kwargs
    assert call.await_args.kwargs["ephemeral_message_parameters"] == {
        "callback_query_id": "q"
    }

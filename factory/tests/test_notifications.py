"""Tests for factory notifications module."""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest

import factory.notifications as notif_module
from factory.notifications import _get_admin_ids, notify


class TestGetAdminIds:
    def test_empty_raw_returns_empty_list(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", ""):
            assert _get_admin_ids() == []

    def test_single_id(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", "123456"):
            assert _get_admin_ids() == [123456]

    def test_multiple_ids(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", "123456,789012"):
            result = _get_admin_ids()
            assert 123456 in result
            assert 789012 in result
            assert len(result) == 2

    def test_ids_with_spaces(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", "123456, 789012 "):
            result = _get_admin_ids()
            assert 123456 in result
            assert 789012 in result

    def test_non_digit_ids_filtered(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", "123456,not_an_id,789012"):
            result = _get_admin_ids()
            assert len(result) == 2
            assert 123456 in result
            assert 789012 in result

    def test_empty_slots_ignored(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", "123456,,789012"):
            result = _get_admin_ids()
            assert len(result) == 2

    def test_returns_list_of_ints(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", "123456"):
            result = _get_admin_ids()
            assert all(isinstance(i, int) for i in result)

    def test_three_ids(self):
        with patch.object(notif_module, "ADMIN_IDS_RAW", "111,222,333"):
            result = _get_admin_ids()
            assert len(result) == 3


class TestNotify:
    def test_notify_no_token_does_not_crash(self):
        with patch.object(notif_module, "BOT_TOKEN", ""), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"):
            notify("test message")  # Should not raise

    def test_notify_no_admins_does_not_crash(self):
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", ""):
            notify("test message")  # Should not raise

    def test_notify_calls_httpx_post(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp) as mock_post:
            notify("hello world")
            mock_post.assert_called_once()

    def test_notify_sends_to_all_admins(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "111,222,333"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp) as mock_post:
            notify("broadcast")
            assert mock_post.call_count == 3

    def test_notify_truncates_long_messages(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        long_msg = "x" * 10000
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp) as mock_post:
            notify(long_msg)
            call_kwargs = mock_post.call_args
            sent_text = call_kwargs[1]["json"]["text"]
            assert len(sent_text) <= 4096

    def test_notify_handles_httpx_exception(self):
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"), \
             patch("factory.notifications.httpx.post", side_effect=Exception("network error")):
            notify("test")  # Should not raise

    def test_notify_handles_api_not_ok(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": False, "description": "Bad Request"}
        mock_resp.text = '{"ok": false}'
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp):
            notify("test")  # Should not raise

    def test_notify_uses_html_parse_mode(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp) as mock_post:
            notify("hello")
            sent_json = mock_post.call_args[1]["json"]
            assert sent_json.get("parse_mode") == "HTML"

    def test_notify_returns_none(self):
        with patch.object(notif_module, "BOT_TOKEN", ""):
            result = notify("test")
            assert result is None

    def test_notify_disables_web_preview(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp) as mock_post:
            notify("hello")
            sent_json = mock_post.call_args[1]["json"]
            assert sent_json.get("disable_web_page_preview") is True

    def test_notify_sends_correct_message_text(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "123456"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp) as mock_post:
            notify("test message content")
            sent_json = mock_post.call_args[1]["json"]
            assert "test message content" in sent_json["text"]

    def test_notify_sends_to_correct_chat_id(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        with patch.object(notif_module, "BOT_TOKEN", "bot123:token"), \
             patch.object(notif_module, "ADMIN_IDS_RAW", "999888"), \
             patch("factory.notifications.httpx.post", return_value=mock_resp) as mock_post:
            notify("test")
            sent_json = mock_post.call_args[1]["json"]
            assert sent_json["chat_id"] == 999888

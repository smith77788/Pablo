"""Tests for factory/notifier.py — Telegram notification helpers."""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest


class TestSendTelegram:
    def test_returns_false_when_no_token(self):
        """send_telegram returns False when BOT_TOKEN is not set."""
        with patch('factory.notifier.TOKEN', ''), \
             patch.dict(os.environ, {}, clear=True):
            from factory.notifier import send_telegram
            result = send_telegram("test message")
            assert result is False

    def test_returns_false_when_no_admin_ids(self):
        """send_telegram returns False when ADMIN_TELEGRAM_IDS is not set."""
        with patch('factory.notifier.TOKEN', 'test:token'), \
             patch('factory.notifier._ADMIN_IDS_RAW', ''):
            from factory.notifier import send_telegram
            result = send_telegram("test message")
            assert result is False

    def test_handles_network_error_gracefully(self):
        """send_telegram does not raise on network error."""
        with patch('factory.notifier.TOKEN', 'test:token'), \
             patch('factory.notifier._ADMIN_IDS_RAW', '12345'), \
             patch('urllib.request.urlopen', side_effect=Exception("Network error")):
            from factory.notifier import send_telegram
            # Should not raise — network errors are swallowed
            try:
                result = send_telegram("test message")
            except Exception as exc:
                pytest.fail(f"send_telegram raised unexpectedly: {exc}")

    def test_returns_true_on_successful_send(self):
        """send_telegram returns True when at least one message is sent."""
        mock_response = MagicMock()
        with patch('factory.notifier.TOKEN', 'test:token'), \
             patch('factory.notifier._ADMIN_IDS_RAW', '12345'), \
             patch('urllib.request.urlopen', return_value=mock_response):
            from factory.notifier import send_telegram
            result = send_telegram("hello")
            assert result is True

    def test_truncates_long_text_to_4096(self):
        """send_telegram truncates text longer than 4096 chars."""
        import json
        long_text = "x" * 5000
        captured_payload: list[bytes] = []

        def fake_urlopen(req, timeout=10):
            captured_payload.append(req.data)
            return MagicMock()

        with patch('factory.notifier.TOKEN', 'test:token'), \
             patch('factory.notifier._ADMIN_IDS_RAW', '12345'), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            from factory.notifier import send_telegram
            send_telegram(long_text)

        assert len(captured_payload) == 1
        payload = json.loads(captured_payload[0])
        assert len(payload["text"]) == 4096

    def test_sends_to_multiple_admin_ids(self):
        """send_telegram sends one message per admin ID."""
        call_count = [0]

        def fake_urlopen(req, timeout=10):
            call_count[0] += 1
            return MagicMock()

        with patch('factory.notifier.TOKEN', 'test:token'), \
             patch('factory.notifier._ADMIN_IDS_RAW', '111,222,333'), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            from factory.notifier import send_telegram
            result = send_telegram("multi admin test")
            assert result is True
            assert call_count[0] == 3

    def test_accepts_custom_parse_mode(self):
        """send_telegram accepts and forwards parse_mode to API."""
        import json
        captured_payload: list[bytes] = []

        def fake_urlopen(req, timeout=10):
            captured_payload.append(req.data)
            return MagicMock()

        with patch('factory.notifier.TOKEN', 'test:token'), \
             patch('factory.notifier._ADMIN_IDS_RAW', '12345'), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            from factory.notifier import send_telegram
            send_telegram("test", parse_mode="Markdown")

        payload = json.loads(captured_payload[0])
        assert payload["parse_mode"] == "Markdown"


class TestNotifyCycleComplete:
    def test_does_not_raise_on_empty_results(self):
        """notify_cycle_complete handles empty results dict without raising."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({})  # Should not raise

    def test_calls_send_telegram(self):
        """notify_cycle_complete calls send_telegram at least once."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({})
            assert mock_send.called

    def test_includes_health_score_in_message(self):
        """notify_cycle_complete includes health score in the message."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({"health_score": 85})
            call_text = mock_send.call_args[0][0]
            assert "85" in call_text

    def test_uses_green_icon_for_high_health(self):
        """notify_cycle_complete uses 💚 icon when health >= 70."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({"health_score": 90})
            call_text = mock_send.call_args[0][0]
            assert "💚" in call_text

    def test_uses_yellow_icon_for_medium_health(self):
        """notify_cycle_complete uses 🟡 icon when 50 <= health < 70."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({"health_score": 55})
            call_text = mock_send.call_args[0][0]
            assert "🟡" in call_text

    def test_uses_red_icon_for_low_health(self):
        """notify_cycle_complete uses 🔴 icon when health < 50."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({"health_score": 30})
            call_text = mock_send.call_args[0][0]
            assert "🔴" in call_text

    def test_includes_decisions_count(self):
        """notify_cycle_complete includes decision count in message."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({
                "decisions": [
                    {"type": "grow", "action": "do something"},
                    {"type": "monitor", "action": "watch"},
                ]
            })
            call_text = mock_send.call_args[0][0]
            assert "2" in call_text

    def test_includes_focus_when_provided(self):
        """notify_cycle_complete includes ceo_department_focus when set."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({"ceo_department_focus": "marketing"})
            call_text = mock_send.call_args[0][0]
            assert "marketing" in call_text

    def test_handles_string_health_score(self):
        """notify_cycle_complete handles non-numeric health score gracefully."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({"health_score": "N/A"})  # Should not raise

    def test_handles_nevesty_models_orders_metrics(self):
        """notify_cycle_complete shows orders metrics when provided."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({
                "nevesty_models": {
                    "orders_7d": 42,
                    "orders_growth_pct": 15,
                }
            })
            call_text = mock_send.call_args[0][0]
            assert "42" in call_text

    def test_handles_channel_content_posts(self):
        """notify_cycle_complete shows post count when phases contain channel_content."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({
                "phases": {
                    "channel_content": {"posts_generated": 3}
                }
            })
            call_text = mock_send.call_args[0][0]
            assert "3" in call_text

    def test_message_is_string(self):
        """notify_cycle_complete passes a string to send_telegram."""
        with patch('factory.notifier.send_telegram') as mock_send:
            from factory.notifier import notify_cycle_complete
            notify_cycle_complete({"health_score": 75, "decisions": []})
            call_text = mock_send.call_args[0][0]
            assert isinstance(call_text, str)

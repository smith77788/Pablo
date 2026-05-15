"""Tests for Phase 25: Channel Publisher."""
import pytest
from unittest.mock import patch, MagicMock
import os

# Import the function from cycle.py
import sys
sys.path.insert(0, '/home/user/Pablo/factory')
from cycle import run_phase_25_channel_publisher


class TestChannelPublisher:
    def test_skips_when_no_channel_id(self):
        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '', 'TELEGRAM_BOT_TOKEN': 'token'}):
            result = run_phase_25_channel_publisher({'tips_post': 'hello'})
        assert result['status'] == 'skipped'

    def test_skips_when_no_token(self):
        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '-100123', 'TELEGRAM_BOT_TOKEN': '', 'BOT_TOKEN': ''}):
            result = run_phase_25_channel_publisher({'tips_post': 'hello'})
        assert result['status'] == 'skipped'

    def test_skips_when_no_content(self):
        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '-100123', 'TELEGRAM_BOT_TOKEN': 'abc'}):
            result = run_phase_25_channel_publisher({})
        assert result['status'] == 'skipped'
        assert result['reason'] == 'no_content'

    def test_publishes_successfully(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":42}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '-100123', 'TELEGRAM_BOT_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen', return_value=mock_resp):
                result = run_phase_25_channel_publisher({'tips_post': 'Test post'})

        assert result['status'] == 'published'
        assert result['message_id'] == 42

    def test_handles_telegram_error(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok":false,"error_code":400,"description":"Bad Request"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '-100123', 'TELEGRAM_BOT_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen', return_value=mock_resp):
                result = run_phase_25_channel_publisher({'tips_post': 'Test'})

        assert result['status'] == 'error'

    def test_handles_network_exception(self):
        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '-100123', 'TELEGRAM_BOT_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen', side_effect=Exception("timeout")):
                result = run_phase_25_channel_publisher({'tips_post': 'Test'})

        assert result['status'] == 'error'
        assert 'timeout' in result['detail']

    def test_truncates_long_content(self):
        """Content longer than 4096 chars should be truncated."""
        long_post = 'x' * 5000
        captured_payloads = []

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        original_request = __import__('urllib.request', fromlist=['Request']).Request

        def capture_request(url, data=None, headers=None, method=None):
            if data:
                import json
                captured_payloads.append(json.loads(data))
            return original_request(url, data=data, headers=headers or {}, method=method)

        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '-100123', 'TELEGRAM_BOT_TOKEN': 'tok'}):
            with patch('urllib.request.Request', side_effect=capture_request):
                with patch('urllib.request.urlopen', return_value=mock_resp):
                    run_phase_25_channel_publisher({'tips_post': long_post})

        if captured_payloads:
            assert len(captured_payloads[0]['text']) <= 4096

    def test_uses_bot_token_fallback(self):
        """Should fall back to BOT_TOKEN if TELEGRAM_BOT_TOKEN not set."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        env = {'TELEGRAM_CHANNEL_ID': '-100123', 'TELEGRAM_BOT_TOKEN': '', 'BOT_TOKEN': 'fallback_token'}
        with patch.dict(os.environ, env):
            with patch('urllib.request.urlopen', return_value=mock_resp):
                result = run_phase_25_channel_publisher({'tips_post': 'hi'})

        assert result['status'] == 'published'

    def test_returns_dict(self):
        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '', 'TELEGRAM_BOT_TOKEN': ''}):
            result = run_phase_25_channel_publisher({'tips_post': 'x'})
        assert isinstance(result, dict)
        assert 'status' in result

    def test_status_field_values(self):
        """Status must be one of published/skipped/error."""
        valid_statuses = {'published', 'skipped', 'error'}
        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '', 'TELEGRAM_BOT_TOKEN': ''}):
            result = run_phase_25_channel_publisher({})
        assert result['status'] in valid_statuses

    def test_skipped_has_reason(self):
        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '', 'TELEGRAM_BOT_TOKEN': 'tok'}):
            result = run_phase_25_channel_publisher({'tips_post': 'hi'})
        assert 'reason' in result

    def test_published_has_channel_field(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok":true,"result":{"message_id":99}}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {'TELEGRAM_CHANNEL_ID': '@testchan', 'TELEGRAM_BOT_TOKEN': 'tok'}):
            with patch('urllib.request.urlopen', return_value=mock_resp):
                result = run_phase_25_channel_publisher({'tips_post': 'hi'})

        assert result.get('channel') == '@testchan'

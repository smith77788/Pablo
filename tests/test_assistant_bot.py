"""Tests for the assistant bot's pure helpers (no network, no anthropic)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ASSISTANT_STATE_DIR", tempfile.mkdtemp(prefix="assistant-test-"))

from assistant.telegram_api import split_message  # noqa: E402
from assistant.state import BotState, DEFAULT_ADMIN_ID, trim_history  # noqa: E402


class TestSplitMessage(unittest.TestCase):
    def test_short_message_untouched(self):
        self.assertEqual(split_message("привет"), ["привет"])

    def test_empty_message(self):
        self.assertEqual(split_message("   "), [])

    def test_long_message_split_under_limit(self):
        text = "\n".join(f"строка {i} " + "x" * 80 for i in range(200))
        parts = split_message(text, limit=4000)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), 4000)
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""),
                         text.replace("\n", "").replace(" ", ""))

    def test_split_prefers_newline(self):
        text = "a" * 3000 + "\n" + "b" * 3000
        parts = split_message(text, limit=4000)
        self.assertEqual(parts[0], "a" * 3000)


class TestTrimHistory(unittest.TestCase):
    def test_no_trim_under_limit(self):
        history = [{"role": "user", "content": "hi"}]
        self.assertEqual(trim_history(history, limit=10), history)

    def test_trim_does_not_start_with_tool_result(self):
        history = []
        for i in range(10):
            history.append({"role": "user", "content": f"q{i}"})
            history.append({"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]})
            history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]})
            history.append({"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]})
        trimmed = trim_history(history, limit=6)
        self.assertLessEqual(len(trimmed), 6)
        first = trimmed[0]
        self.assertEqual(first["role"], "user")
        self.assertNotIsInstance(first["content"], list)


class TestBotState(unittest.TestCase):
    def test_default_admin_always_present(self):
        state = BotState()
        self.assertTrue(state.is_admin(DEFAULT_ADMIN_ID))

    def test_grant_and_revoke(self):
        state = BotState()
        state.grant(123)
        self.assertTrue(state.is_admin(123))
        self.assertTrue(state.revoke(123))
        self.assertFalse(state.is_admin(123))

    def test_owner_cannot_be_revoked(self):
        state = BotState()
        self.assertFalse(state.revoke(DEFAULT_ADMIN_ID))
        self.assertTrue(state.is_admin(DEFAULT_ADMIN_ID))

    def test_history_roundtrip(self):
        state = BotState()
        state.update_history(42, [{"role": "user", "content": "hi"},
                                  {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}])
        self.assertEqual(len(state.history(42)), 2)
        state.reset_history(42)
        self.assertEqual(state.history(42), [])


if __name__ == "__main__":
    unittest.main()

"""Tests for factory structured JSON logging (БЛОК 7.3)."""
import io
import json
import logging
import os
import sys

import pytest

from factory.logging_config import JSONFormatter, configure_logging


class TestJSONFormatter:
    def _make_record(self, msg: str = "hello", level: int = logging.INFO, name: str = "test") -> logging.LogRecord:
        record = logging.LogRecord(name=name, level=level, pathname="", lineno=0, msg=msg, args=(), exc_info=None)
        return record

    def test_format_returns_string(self):
        fmt = JSONFormatter()
        record = self._make_record("test message")
        result = fmt.format(record)
        assert isinstance(result, str)

    def test_format_is_valid_json(self):
        fmt = JSONFormatter()
        record = self._make_record("test message")
        result = fmt.format(record)
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_format_has_ts_field(self):
        fmt = JSONFormatter()
        record = self._make_record()
        parsed = json.loads(fmt.format(record))
        assert "ts" in parsed

    def test_format_has_level_field(self):
        fmt = JSONFormatter()
        record = self._make_record(level=logging.WARNING)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "WARNING"

    def test_format_has_msg_field(self):
        fmt = JSONFormatter()
        record = self._make_record("my log message")
        parsed = json.loads(fmt.format(record))
        assert parsed["msg"] == "my log message"

    def test_format_has_logger_field(self):
        fmt = JSONFormatter()
        record = self._make_record(name="factory.cycle")
        parsed = json.loads(fmt.format(record))
        assert parsed["logger"] == "factory.cycle"

    def test_format_ts_is_iso_format(self):
        fmt = JSONFormatter()
        record = self._make_record()
        parsed = json.loads(fmt.format(record))
        ts = parsed["ts"]
        assert "T" in ts  # ISO 8601 format

    def test_format_level_info(self):
        fmt = JSONFormatter()
        record = self._make_record(level=logging.INFO)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "INFO"

    def test_format_level_error(self):
        fmt = JSONFormatter()
        record = self._make_record(level=logging.ERROR)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "ERROR"

    def test_format_level_debug(self):
        fmt = JSONFormatter()
        record = self._make_record(level=logging.DEBUG)
        parsed = json.loads(fmt.format(record))
        assert parsed["level"] == "DEBUG"

    def test_format_custom_phase_attribute(self):
        fmt = JSONFormatter()
        record = self._make_record()
        record.phase = "analytics"
        parsed = json.loads(fmt.format(record))
        assert parsed.get("phase") == "analytics"

    def test_format_custom_dept_attribute(self):
        fmt = JSONFormatter()
        record = self._make_record()
        record.dept = "marketing"
        parsed = json.loads(fmt.format(record))
        assert parsed.get("dept") == "marketing"

    def test_format_no_phase_when_not_set(self):
        fmt = JSONFormatter()
        record = self._make_record()
        parsed = json.loads(fmt.format(record))
        assert "phase" not in parsed

    def test_format_unicode_message(self):
        fmt = JSONFormatter()
        record = self._make_record("Тест сообщение")
        result = fmt.format(record)
        parsed = json.loads(result)
        assert "Тест" in parsed["msg"]

    def test_format_special_chars_in_message(self):
        fmt = JSONFormatter()
        record = self._make_record('message with "quotes" and \\backslash')
        result = fmt.format(record)
        assert json.loads(result)  # Should be valid JSON


class TestConfigureLogging:
    def test_configure_logging_runs_without_error(self):
        configure_logging()

    def test_configure_logging_sets_level_info(self):
        configure_logging(level="INFO")
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_configure_logging_accepts_debug_level(self):
        configure_logging(level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        configure_logging(level="INFO")  # reset

    def test_configure_logging_accepts_warning_level(self):
        configure_logging(level="WARNING")
        root = logging.getLogger()
        assert root.level == logging.WARNING
        configure_logging(level="INFO")  # reset

    def test_json_mode_via_env(self, monkeypatch):
        monkeypatch.setenv("LOG_JSON", "1")
        # Reset handlers to test configure
        root = logging.getLogger()
        old_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            configure_logging()
            assert root.handlers
            # Check formatter is JSONFormatter
            assert isinstance(root.handlers[0].formatter, JSONFormatter)
        finally:
            root.handlers.clear()
            root.handlers.extend(old_handlers)

    def test_text_mode_when_no_json_env(self, monkeypatch):
        monkeypatch.delenv("LOG_JSON", raising=False)
        root = logging.getLogger()
        old_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            configure_logging()
            if root.handlers:
                assert not isinstance(root.handlers[0].formatter, JSONFormatter)
        finally:
            root.handlers.clear()
            root.handlers.extend(old_handlers)

    def test_configure_logging_returns_none(self):
        result = configure_logging()
        assert result is None

    def test_httpx_logger_set_to_warning(self):
        configure_logging()
        httpx_logger = logging.getLogger("httpx")
        assert httpx_logger.level == logging.WARNING

    def test_anthropic_logger_set_to_warning(self):
        configure_logging()
        anth_logger = logging.getLogger("anthropic")
        assert anth_logger.level == logging.WARNING

    def test_log_json_true_string(self, monkeypatch):
        monkeypatch.setenv("LOG_JSON", "true")
        root = logging.getLogger()
        old_handlers = root.handlers[:]
        root.handlers.clear()
        try:
            configure_logging()
            assert isinstance(root.handlers[0].formatter, JSONFormatter)
        finally:
            root.handlers.clear()
            root.handlers.extend(old_handlers)

    def test_configure_logging_idempotent_with_handlers(self):
        root = logging.getLogger()
        initial_count = len(root.handlers)
        configure_logging()
        configure_logging()
        # Should not add duplicate handlers (no-op when handlers exist)
        assert len(root.handlers) <= initial_count + 1

"""Публикация в канал и массовые жалобы: сбои видны и понятны (регресс).

Было: FloodWait при публикации не попадал в пульс аккаунта (следующая
публикация снова брала его), строка итога писала сырой английский текст
Telegram, а исключение в массовых жалобах не оставляло строки в журнале.
"""
from __future__ import annotations

import inspect

from services import op_worker


def test_bulk_post_records_flood_and_explains_errors():
    src = inspect.getsource(op_worker._exec_bulk_post_to_channel)
    assert "record_flood(" in src
    assert "explain_error_short(err_str)" in src
    assert "explain_error_short(str(_post_exc))" in src


def test_mass_report_exception_is_journaled():
    src = inspect.getsource(op_worker._exec_mass_report)
    i = src.index("except Exception as exc:")
    assert "INSERT INTO operation_log" in src[i:i + 900]

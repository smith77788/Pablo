"""Чистая аналитика диалогов Хранилища — без БД."""
from __future__ import annotations

import datetime as dt

from services.vault_analytics import analyze_dialogs


def _d(y, mo, day, h, mi=0):
    return dt.datetime(y, mo, day, h, mi, tzinfo=dt.timezone.utc)


def test_empty():
    a = analyze_dialogs([])
    assert a["total"] == 0 and a["dialogs"] == 0
    assert a["reply_rate_pct"] is None and a["median_response_min"] is None
    assert a["busiest_hour"] is None


def test_counts_and_directions():
    rows = [
        {"chat_id": 1, "direction": "in", "msg_date": _d(2026, 1, 1, 10)},
        {"chat_id": 1, "direction": "out", "msg_date": _d(2026, 1, 1, 10, 5)},
        {"chat_id": 2, "direction": "in", "msg_date": _d(2026, 1, 1, 12)},
    ]
    a = analyze_dialogs(rows)
    assert a["total"] == 3 and a["incoming"] == 2 and a["outgoing"] == 1
    assert a["dialogs"] == 2


def test_reply_rate_and_response_time():
    rows = [
        # диалог 1: ответили через 5 мин
        {"chat_id": 1, "direction": "in", "msg_date": _d(2026, 1, 1, 10)},
        {"chat_id": 1, "direction": "out", "msg_date": _d(2026, 1, 1, 10, 5)},
        # диалог 2: ответили через 15 мин
        {"chat_id": 2, "direction": "in", "msg_date": _d(2026, 1, 1, 11)},
        {"chat_id": 2, "direction": "out", "msg_date": _d(2026, 1, 1, 11, 15)},
        # диалог 3: не ответили
        {"chat_id": 3, "direction": "in", "msg_date": _d(2026, 1, 1, 12)},
    ]
    a = analyze_dialogs(rows)
    assert a["replied_dialogs"] == 2
    assert a["reply_rate_pct"] == round(2 / 3 * 100, 1)      # 3 диалога с входящим
    assert a["median_response_min"] == 10.0                   # медиана(5,15)


def test_out_before_in_not_counted_as_reply():
    # исходящее раньше входящего — не ответ на это входящее
    rows = [
        {"chat_id": 1, "direction": "out", "msg_date": _d(2026, 1, 1, 9)},
        {"chat_id": 1, "direction": "in", "msg_date": _d(2026, 1, 1, 10)},
    ]
    a = analyze_dialogs(rows)
    assert a["replied_dialogs"] == 0
    assert a["reply_rate_pct"] == 0.0


def test_busiest_hour():
    rows = [
        {"chat_id": 1, "direction": "in", "msg_date": _d(2026, 1, 1, 14)},
        {"chat_id": 2, "direction": "in", "msg_date": _d(2026, 1, 1, 14)},
        {"chat_id": 3, "direction": "in", "msg_date": _d(2026, 1, 1, 9)},
    ]
    assert analyze_dialogs(rows)["busiest_hour"] == 14


def test_epoch_and_none_dates_safe():
    rows = [
        {"chat_id": 1, "direction": "in", "msg_date": 1735725600},  # epoch
        {"chat_id": 1, "direction": "out", "msg_date": None},        # без даты
    ]
    a = analyze_dialogs(rows)
    assert a["total"] == 2 and a["incoming"] == 1 and a["outgoing"] == 1
    # out без даты не даёт reply-time
    assert a["median_response_min"] is None

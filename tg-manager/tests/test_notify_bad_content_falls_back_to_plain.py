"""Отчёт с битым HTML или длиной >4096 обязан ДОЙТИ простым текстом.

Симптом из прода (14.11.2026): «отчёт об операции не доехал до владельца — N
раз» с растущим N, тогда как короткий мета-алерт «не доставлено» доходил.
Причина: Telegram отклонял САМ ТЕКСТ отчёта («can't parse entities» или
«message is too long»), а доставка считала это временным сбоем — ретраила
тем же текстом, копила счётчик недоставки и слала отчёт заново каждый цикл
воркера. Владелец отчёт не получал вообще.

Фикс: при ошибке разметки/длины — переслать простым текстом (усечённо), и
классифицировать такую ошибку отдельно (не «временный сбой»).
"""
from __future__ import annotations

import asyncio

import pytest

from database import db


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    async def _settings(pool, uid):
        return {}
    async def _no_hold(pool, uid, pref, *a, **k):
        return False
    monkeypatch.setattr(db, "get_notification_settings", _settings)
    from services import quiet_hours as qh
    monkeypatch.setattr(qh, "should_hold", _no_hold)
    db._notify_cooldown.clear()


class _Bot:
    """Шлёт: с parse_mode='HTML' кидает ошибку разметки, без него — успех."""
    def __init__(self, fail_plain=False):
        self.calls = []
        self.fail_plain = fail_plain

    async def send_message(self, uid, text, parse_mode=None, reply_markup=None):
        self.calls.append({"text": text, "parse_mode": parse_mode})
        if parse_mode == "HTML":
            raise RuntimeError("Bad Request: can't parse entities: unexpected end tag")
        if self.fail_plain:
            raise RuntimeError("Bad Request: can't parse entities")
        return True


def test_malformed_html_report_is_delivered_as_plain_text():
    bot = _Bot()
    ok = asyncio.run(db.notify_if_enabled(None, bot, 101, "reports",
                                          "Итог <b>операции</b: 5 &amp; 3"))
    assert ok is True, "доставка должна считаться успешной"
    # Был аварийный повтор БЕЗ parse_mode, и он доставлен.
    plain = [c for c in bot.calls if c["parse_mode"] != "HTML"]
    assert plain, "не было отправки простым текстом — владелец отчёт не получил"
    # В простом тексте нет тегов и сущности развёрнуты.
    assert "<b>" not in plain[0]["text"] and "&amp;" not in plain[0]["text"]


def test_too_long_report_is_truncated_and_sent():
    bot = _Bot()
    long_text = "А" * 9000
    ok = asyncio.run(db.notify_if_enabled(None, bot, 102, "reports", long_text))
    assert ok is True
    plain = [c for c in bot.calls if c["parse_mode"] != "HTML"]
    assert plain and len(plain[0]["text"]) <= db._TG_MESSAGE_LIMIT


def test_content_error_is_not_counted_as_transient(monkeypatch):
    # Фолбэк простым текстом ТОЖЕ падает → считается как bad_content, не как
    # временная недоставка, и доставку не ретраим (return True).
    seen = []
    from services import metrics as m
    monkeypatch.setattr(m, "inc", lambda name, labels=None: seen.append(name))
    bot = _Bot(fail_plain=True)
    ok = asyncio.run(db.notify_if_enabled(None, bot, 103, "reports", "<b>x</b"))
    assert ok is True, "битый текст повторять бессмысленно — не просим ретрай"
    assert "infragram_notifications_bad_content_total" in seen
    assert "infragram_notifications_undelivered_total" not in seen


def test_network_error_still_transient(monkeypatch):
    # Обычный сетевой сбой остаётся временным (worth retrying → False).
    seen = []
    from services import metrics as m
    monkeypatch.setattr(m, "inc", lambda name, labels=None: seen.append(name))

    class _Net:
        async def send_message(self, *a, **k):
            raise RuntimeError("Connection reset by peer")
    ok = asyncio.run(db.notify_if_enabled(None, _Net(), 104, "reports", "ok"))
    assert ok is False, "сетевой сбой — временный, стоит повторить"
    assert "infragram_notifications_undelivered_total" in seen


def test_html_to_plain_strips_tags_and_unescapes():
    out = db._html_to_plain("Строка<br>вторая <b>жирным</b> 5 &lt; 7 &amp; 8")
    assert "<b>" not in out and "<br>" not in out
    assert "\n" in out and "5 < 7 & 8" in out

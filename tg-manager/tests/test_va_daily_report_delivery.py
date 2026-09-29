"""Суточный отчёт администратора не должен теряться при сбое доставки.

Раньше last_report_at обновлялся ДО bot.send_message. Любой сбой доставки —
владелец заблокировал бота, таймаут, сеть — помечал отчёт отправленным, и
следующая попытка была только через сутки. Отчёт молча пропадал.

Теперь метка ставится по ИТОГУ отправки: успех → сутки, сбой → повтор примерно
через час (метка сдвигается назад, но не настолько, чтобы долбить каждую
минуту цикла).
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import channel_admin_runner as runner  # noqa: E402


class _Pool:
    def __init__(self):
        self.stamps = []  # (owner_id, sql-фрагмент времени)

    async def fetch(self, sql, *a):
        if "DISTINCT owner_id" in sql:
            return [{"owner_id": 42}]
        if "a.channel_id, a.last_error" in sql:
            return [{"channel_id": 100, "last_error": "", "title": "Канал"}]
        return []

    async def execute(self, sql, *a):
        if "SET last_report_at=" in sql:
            frag = sql.split("SET last_report_at=", 1)[1].split(" ", 1)[0]
            # хвост запроса содержит время; берём кусок до WHERE
            when = sql.split("SET last_report_at=", 1)[1].split("WHERE", 1)[0].strip()
            self.stamps.append((a[0], when))


class _Bot:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send_message(self, owner_id, text, **kw):
        if self.fail:
            raise RuntimeError("bot blocked by user")
        self.sent.append((owner_id, text))


def _report_stub():
    async def _rep(pool, owner_id, channel_id):
        return {"posts_7d": 3, "avg_views_7d": 100, "posts_total": 10,
                "members": 500, "members_delta_7d": 5, "best_pillar": "Польза",
                "pillars": []}
    return _rep


def _fmt_stub(items):
    return "отчёт"


def test_successful_report_is_stamped_for_a_full_day(monkeypatch):
    monkeypatch.setattr(runner.ca, "channel_report", _report_stub())
    monkeypatch.setattr(runner.ca, "format_daily_report", _fmt_stub)
    pool, bot = _Pool(), _Bot(fail=False)
    sent = asyncio.run(runner._daily_reports(pool, bot))
    assert sent == 1
    assert bot.sent, "отчёт не отправлен"
    assert pool.stamps and pool.stamps[-1][1] == "now()", (
        "успешный отчёт должен помечаться на сутки"
    )


def test_failed_delivery_retries_within_an_hour_not_a_day(monkeypatch):
    monkeypatch.setattr(runner.ca, "channel_report", _report_stub())
    monkeypatch.setattr(runner.ca, "format_daily_report", _fmt_stub)
    pool, bot = _Pool(), _Bot(fail=True)
    sent = asyncio.run(runner._daily_reports(pool, bot))
    assert sent == 0
    assert not bot.sent
    # метка выставлена, но на повтор через час, а не на сутки
    assert pool.stamps, "при сбое доставки метка не выставлена — отчёт долбит каждую минуту"
    assert "23 hours" in pool.stamps[-1][1], (
        "сбой доставки пометил отчёт как отправленный — он потеряется на сутки"
    )


def test_report_has_a_network_header_for_multiple_channels():
    from services import channel_admin as ca

    items = [
        ("Канал А", {"posts_7d": 5, "members": 1000, "last_error": ""}),
        ("Канал Б", {"posts_7d": 3, "members": 500, "last_error": "нет прав"}),
    ]
    out = ca.format_daily_report(items)
    assert "По сети:" in out
    assert "каналов 2" in out
    assert "постов за неделю 8" in out
    assert "подписчиков 1500" in out
    assert "требуют внимания: 1" in out


def test_single_channel_report_has_no_network_header():
    from services import channel_admin as ca

    out = ca.format_daily_report([("Канал", {"posts_7d": 5, "members": 1000})])
    assert "По сети:" not in out


def test_no_channels_stamps_to_avoid_per_minute_scan(monkeypatch):
    """У владельца нет готовых отчётов по каналам — помечаем на сутки, чтобы
    не перебирать его каждую минуту цикла."""
    async def _boom(pool, owner_id, channel_id):
        raise RuntimeError("нет данных")
    monkeypatch.setattr(runner.ca, "channel_report", _boom)
    pool, bot = _Pool(), _Bot(fail=False)
    sent = asyncio.run(runner._daily_reports(pool, bot))
    assert sent == 0
    assert pool.stamps and pool.stamps[-1][1] == "now()"

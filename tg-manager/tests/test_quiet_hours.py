"""Тихие часы уведомлений: несрочное ночью — в сводку, срочное — сразу.

Проверяется то, на чём тишина могла бы соврать или навредить:
* окно через полночь (23–8) и по местному поясу, а не по UTC;
* ограничения и флуд аккаунтов не придерживаются никогда;
* сбой чтения настроек или очереди не глушит уведомление;
* сводка приходит одна, со счётом скрытого, и только когда окно кончилось;
  временный сбой отправки сводку не теряет.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

from database import db
from services import quiet_hours as qh

from tests.miniapp_source import api_source, miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UTC = dt.timezone.utc


def _utc(h, m=0):
    return dt.datetime(2026, 11, 2, h, m, tzinfo=UTC)


def test_window_crosses_midnight_in_local_time():
    # Москва = UTC+3: 21:00 UTC — это полночь по Москве.
    assert qh.in_quiet(_utc(21), 23, 8, "Europe/Moscow") is True
    assert qh.in_quiet(_utc(4, 59), 23, 8, "Europe/Moscow") is True     # 07:59
    assert qh.in_quiet(_utc(5), 23, 8, "Europe/Moscow") is False        # 08:00
    assert qh.in_quiet(_utc(19, 59), 23, 8, "Europe/Moscow") is False   # 22:59
    # Тот же момент в другом поясе — другое решение.
    assert qh.in_quiet(_utc(21), 23, 8, "America/New_York") is False    # 16:00


def test_window_within_a_day_and_empty_window():
    assert qh.in_quiet(_utc(1), 0, 9, "UTC") is True
    assert qh.in_quiet(_utc(9), 0, 9, "UTC") is False
    assert qh.in_quiet(_utc(3), 5, 5, "UTC") is False


def test_unknown_tz_falls_back_and_is_not_stored():
    assert qh.tz_or_default("Mars/Olympus").key == qh.DEFAULT_TZ
    assert qh.valid_tz("Mars/Olympus") is None
    assert qh.valid_tz("Asia/Almaty") == "Asia/Almaty"
    assert qh.valid_tz("") is None


class _SettingsPool:
    def __init__(self, row=None, fail=False):
        self.row, self.fail = row, fail
        self.held = []

    async def fetchrow(self, sql, *a):
        if self.fail:
            raise RuntimeError("column quiet_enabled does not exist")
        return self.row

    async def fetchval(self, sql, *a):
        return len(self.held)

    async def execute(self, sql, *a):
        if "INSERT INTO notification_held" in sql:
            self.held.append(a)


_NIGHT = {"quiet_enabled": True, "quiet_start": 23, "quiet_end": 8, "tz": "UTC"}


def test_urgent_is_never_held():
    pool = _SettingsPool(_NIGHT)
    for pref in qh.URGENT:
        assert asyncio.run(qh.should_hold(pool, 1, pref, _utc(2))) is False
    assert asyncio.run(qh.should_hold(pool, 1, "op_complete", _utc(2))) is True
    assert asyncio.run(qh.should_hold(pool, 1, "op_complete", _utc(12))) is False


def test_broken_settings_never_silence():
    assert asyncio.run(qh.should_hold(_SettingsPool(fail=True), 1, "new_user", _utc(2))) is False
    assert asyncio.run(qh.should_hold(_SettingsPool(None), 1, "new_user", _utc(2))) is False
    off = dict(_NIGHT, quiet_enabled=False)
    assert asyncio.run(qh.should_hold(_SettingsPool(off), 1, "new_user", _utc(2))) is False
    # «Что-то истинное», а не True — не тишина.
    odd = dict(_NIGHT, quiet_enabled="yes")
    assert asyncio.run(qh.should_hold(_SettingsPool(odd), 1, "new_user", _utc(2))) is False


class _Bot:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    async def send_message(self, uid, text, **kw):
        if self.fail:
            raise self.fail
        self.sent.append((uid, text))


def test_notify_if_enabled_holds_at_night_and_sends_urgent(monkeypatch):
    db._notify_cooldown.clear()

    async def _settings(pool, uid):
        return {}
    monkeypatch.setattr(db, "get_notification_settings", _settings)

    async def _hold_yes(pool, uid, pref, now_utc=None):
        return pref not in qh.URGENT
    monkeypatch.setattr(qh, "should_hold", _hold_yes)
    pool, bot = _SettingsPool(), _Bot()
    assert asyncio.run(db.notify_if_enabled(pool, bot, 7, "op_complete", "<b>Готово</b>")) is True
    assert bot.sent == [] and len(pool.held) == 1
    assert asyncio.run(db.notify_if_enabled(pool, bot, 7, "restriction", "Бан")) is True
    assert bot.sent == [(7, "Бан")]
    db._notify_cooldown.clear()


def test_failed_hold_sends_normally(monkeypatch):
    db._notify_cooldown.clear()

    async def _settings(pool, uid):
        return {}
    monkeypatch.setattr(db, "get_notification_settings", _settings)

    async def _yes(pool, uid, pref, now_utc=None):
        return True

    async def _no(pool, uid, pref, text):
        return False
    monkeypatch.setattr(qh, "should_hold", _yes)
    monkeypatch.setattr(qh, "hold", _no)
    bot = _Bot()
    assert asyncio.run(db.notify_if_enabled(_SettingsPool(), bot, 7, "new_user", "Новый")) is True
    assert bot.sent == [(7, "Новый")]
    db._notify_cooldown.clear()


def test_plain_line_strips_markup_and_caps():
    assert qh.plain_line("<b>Итог</b>\n  операции &amp; <i>всё</i>") == "Итог операции & всё"
    assert len(qh.plain_line("x" * 500)) == qh.DIGEST_LINE_MAX


def _held(n, pref="op_complete"):
    t = dt.datetime(2026, 11, 2, 3, 15)
    return [{"id": i + 1, "pref": pref, "text": f"<b>op {i}</b>", "created_at": t}
            for i in range(n)]


def test_digest_counts_what_it_hides():
    rows = _held(8) + _held(3, "new_user")
    d = qh.build_digest(rows, "23:00–08:00")
    assert "уведомлений: 11" in d
    assert "Операции</b> · 8" in d and "…и ещё 3" in d
    assert "<b>op" not in d          # разметка уведомлений не протекает в сводку
    assert "03:15" in d


class _FlushPool:
    def __init__(self, owners, rows):
        self.owners, self.rows, self.deleted = owners, rows, []

    async def fetch(self, sql, *a):
        if "SELECT DISTINCT" in sql:
            return self.owners
        return self.rows

    async def execute(self, sql, *a):
        if sql.startswith("DELETE"):
            self.deleted.append(a)


_OWNER = {"user_id": 7, "quiet_enabled": True, "quiet_start": 23, "quiet_end": 8, "tz": "UTC"}


def test_flush_waits_for_the_window_to_end():
    pool, bot = _FlushPool([_OWNER], _held(2)), _Bot()
    assert asyncio.run(qh.flush_once(pool, bot, _utc(3))) == 0
    assert bot.sent == [] and pool.deleted == []
    assert asyncio.run(qh.flush_once(pool, bot, _utc(9))) == 1
    assert len(bot.sent) == 1 and pool.deleted == [(7, 2)]


def test_flush_keeps_queue_on_temporary_failure():
    pool = _FlushPool([_OWNER], _held(2))
    bot = _Bot(fail=TimeoutError("network"))
    assert asyncio.run(qh.flush_once(pool, bot, _utc(9))) == 0
    assert pool.deleted == []


def test_wired_everywhere():
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert '_resilient("quiet_hours", _quiet_hours.run, pool, bot)' in main
    src = api_source()
    assert '"quiet_enabled" in data' in src and "notification_held" in src
    html = miniapp_html()
    assert 'id="set-quiet"' in html and 'id="set-quiet-window"' in html
    assert "resolvedOptions().timeZone" in html
    bm = open(os.path.join(ROOT, "bot", "handlers", "botmother_menu.py"), encoding="utf-8").read()
    assert 'BmCb.filter(F.action == "notif_quiet")' in bm
    name = "schema_v291_notification_quiet_hours.sql"
    assert os.path.exists(os.path.join(ROOT, name))
    assert name in open(os.path.join(ROOT, "schema_checksums.txt"), encoding="utf-8").read()

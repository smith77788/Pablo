"""Уровни уведомлений и фоновые сервисы через общий слой.

Проверяется:
* у каждого типа есть уровень: «нужно действие» идёт сразу и ночью, «инфо»
  уважает тихие часы и «несрочное — одной сводкой в день»;
* сводка за день приходит в свой час и не чаще раза в сутки по местному
  календарю;
* лиды, защита аккаунтов, волна банов, отчёты и советы фоновых сервисов
  больше не шлются прямым send_message мимо настроек;
* тумблеры есть и в боте, и в мини-аппе; на живой схеме выключенный тип
  читается как выключенный.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import re

import pytest

from database import db
from services import quiet_hours as qh

from tests.miniapp_source import api_source, miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
UTC = dt.timezone.utc


def _utc(d, h, m=0):
    return dt.datetime(2026, 11, d, h, m, tzinfo=UTC)


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_every_toggle_has_a_level():
    for pref in db.NOTIFY_PREFS:
        assert pref in qh.LEVELS, pref
    assert qh.level_of("leads") == qh.ACTION
    assert qh.level_of("restriction") == qh.ACTION
    assert qh.level_of("reports") == qh.INFO
    assert qh.level_of("что-то новое") == qh.INFO   # неизвестное не будит
    assert "leads" in qh.URGENT


class _SettingsPool:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *a):
        return self.row


def test_digest_only_holds_info_but_never_action():
    row = {"quiet_enabled": False, "quiet_start": 23, "quiet_end": 8, "tz": "UTC",
           "digest_only": True}
    pool = _SettingsPool(row)
    assert asyncio.run(qh.should_hold(pool, 7, "op_complete", _utc(2, 14))) is True
    assert asyncio.run(qh.should_hold(pool, 7, "reports", _utc(2, 14))) is True
    assert asyncio.run(qh.should_hold(pool, 7, "leads", _utc(2, 14))) is False
    assert asyncio.run(qh.should_hold(pool, 7, "restriction", _utc(2, 3))) is False
    row["digest_only"] = False
    assert asyncio.run(qh.should_hold(pool, 7, "op_complete", _utc(2, 14))) is False


def test_daily_digest_is_due_once_a_day_at_its_hour():
    # Москва UTC+3, час сводки 9: 05:59 UTC — 08:59, рано.
    assert qh.digest_due(_utc(2, 5, 59), 9, None, "Europe/Moscow") is False
    assert qh.digest_due(_utc(2, 6), 9, None, "Europe/Moscow") is True
    # Сегодня уже была — до завтра нет.
    assert qh.digest_due(_utc(2, 15), 9, _utc(2, 6, 5), "Europe/Moscow") is False
    assert qh.digest_due(_utc(3, 6), 9, _utc(2, 6, 5), "Europe/Moscow") is True


class _FlushPool:
    def __init__(self, owner, rows):
        self.owner, self.rows, self.executed = owner, rows, []

    async def fetch(self, sql, *a):
        return [self.owner] if "SELECT DISTINCT" in sql else self.rows

    async def execute(self, sql, *a):
        self.executed.append(sql.split()[0])


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text))


def test_flush_sends_the_daily_digest_at_its_hour_and_stamps_it():
    owner = {"held_uid": 7, "quiet_enabled": False, "quiet_start": 23, "quiet_end": 8,
             "tz": "UTC", "digest_only": True, "digest_hour": 9, "last_digest_at": None}
    rows = [{"id": 1, "pref": "reports", "text": "<b>Отчёт</b>",
             "created_at": dt.datetime(2026, 11, 2, 7, 0)}]
    pool, bot = _FlushPool(owner, rows), _Bot()
    assert asyncio.run(qh.flush_once(pool, bot, _utc(2, 8))) == 0 and bot.sent == []
    assert asyncio.run(qh.flush_once(pool, bot, _utc(2, 9))) == 1
    assert "Сводка за день" in bot.sent[0][1] and "Отчёты" in bot.sent[0][1]
    assert pool.executed == ["DELETE", "UPDATE"]


# Функция → тип уведомления, через который она обязана слать.
_ROUTED = {
    "services/account_shield.py": ("async def _notify_pause", '"restriction"'),
    "services/anti_storm.py": ("async def check_and_arm", '"restriction"'),
    "services/workflow_runtime.py": ("async def _do_action", '"leads"'),
    "services/intent_sensor.py": ("async def scan_incoming", '"leads"'),
    "services/channel_admin_runner.py": ("async def _daily_reports", '"reports"'),
    "services/vault_watchdog.py": ("async def _check_once", '"advice"'),
    "services/organism/runner.py": ("async def _tick_owner", '"advice"'),
    "services/promo_scheduler.py": ("async def _notify", '"reports"'),
}


def _fn(src, head):
    i = src.index(head)
    m = re.search(r"\n(async def |def |@)", src[i + len(head):])
    return src[i:i + len(head) + (m.start() if m else len(src))]


def test_background_notices_go_through_the_layer():
    for rel, (head, pref) in _ROUTED.items():
        src = _src(rel)
        body = _fn(src, head)
        assert "notify_if_enabled(" in body and pref in body, f"{rel}: мимо слоя"
        assert not re.search(r"\bbot\.send_message\(", body), f"{rel}: прямая отправка"


def test_toggles_are_in_the_bot_and_the_app():
    bm = _src("bot/handlers/botmother_menu.py")
    for pref in ("leads", "reports", "advice", "digest_only"):
        assert f'"{pref}": "{pref}' in bm, pref
    html = miniapp_html()
    for el in ("set-notif-leads", "set-notif-reports", "set-notif-advice", "set-digest-only"):
        assert f'id="{el}"' in html, el
    api = api_source()
    assert 'settings["notif_leads"]' in api and '"digest_only" in data' in api


def test_defaults_without_a_row_are_all_on():
    class _P:
        async def fetchrow(self, *a):
            return None
    s = asyncio.run(db.get_notification_settings(_P(), 1))
    assert all(s[p] for p in db.NOTIFY_PREFS)


def test_live_schema_reads_a_disabled_type():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    uid = 88_500_001

    async def _go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        try:
            await pool.execute(_src("schema_v312_notification_levels.sql"))
            await pool.execute("DELETE FROM notification_settings WHERE user_id=$1", uid)
            await pool.execute(
                "INSERT INTO notification_settings(user_id, leads, digest_only) "
                "VALUES ($1, FALSE, TRUE)", uid)
            s = await db.get_notification_settings(pool, uid)
            assert s["leads"] is False and s["reports"] is True
            assert await qh.should_hold(pool, uid, "reports") is True
            assert await qh.should_hold(pool, uid, "restriction") is False
            await qh.save_digest(pool, uid, False, 10)
            assert await qh.should_hold(pool, uid, "reports") is False
        finally:
            await pool.execute("DELETE FROM notification_settings WHERE user_id=$1", uid)
            await pool.close()
    asyncio.run(_go())

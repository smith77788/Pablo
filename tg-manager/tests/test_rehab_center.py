"""Центр восстановления аккаунтов: дата снятия, отдых, настройки, история.

Что было не так (каждый тест падает на коде до исправления):

• Временный блок называет дату снятия («released on 13 Nov 2026»), но её не
  читали: перепроверки шли по слепому бэк-оффу, и блок на месяц уходил «на
  разбор» с советом заменить аккаунт за недели до названной даты.
• После снятия аккаунт сразу шёл в операции — совет «не нагружайте сразу»
  в уведомлении ничего не делал, и блок возвращался.
• Темп, лимит перепроверок, аппеляция и уведомления были зашиты в код.
• Что делала автоматика, было не видно: одна строка «заметка», которую
  затирал каждый шаг.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from services import account_rehab as ar

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Pool:
    def __init__(self, settings=None):
        self.execs: list[tuple] = []
        self.settings = settings

    async def execute(self, sql, *args):
        self.execs.append((sql, args))
        return "OK"

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        if "FROM rehab_settings" in sql:
            return self.settings
        return None

    def advance(self):
        for sql, args in reversed(self.execs):
            if "UPDATE account_rehab_state SET" in sql:
                return args
        raise AssertionError("не было перехода фазы")

    def steps(self):
        return [a[2] for sql, a in self.execs if "INSERT INTO account_rehab_events" in sql]


# ── Дата снятия ──────────────────────────────────────────────────────────────
def test_release_date_is_read_from_english_reply():
    txt = ("I'm afraid some Telegram users found your messages annoying and "
           "forwarded them to our team of moderators for inspection. ... Your "
           "account will be automatically released on 13 Nov 2026, 15:42 UTC.")
    assert ar.parse_release_at(txt, NOW) == datetime(2026, 11, 13, 15, 42, tzinfo=timezone.utc)


def test_release_date_is_read_from_russian_reply():
    txt = "Ограничения будут автоматически сняты 25 ноября 2026 г., 08:05 UTC."
    assert ar.parse_release_at(txt, NOW) == datetime(2026, 11, 25, 8, 5, tzinfo=timezone.utc)


def test_release_date_other_formats():
    assert ar.parse_release_at("until 2026-12-01 10:00", NOW).day == 1
    assert ar.parse_release_at("до 03.12.2026", NOW).month == 12
    assert ar.parse_release_at("released on November 20, 2026", NOW).day == 20


def test_release_date_noise_is_not_a_date():
    assert ar.parse_release_at("", NOW) is None
    assert ar.parse_release_at("Unfortunately, your account is now limited.", NOW) is None
    # регистрация аккаунта в 2019 — не дата снятия
    assert ar.parse_release_at("since 12 Mar 2019", NOW) is None


# ── Цикл: дата снятия не даёт уйти «на разбор» ───────────────────────────────
def _recheck(monkeypatch, chk, **row):
    async def fake_check(sess, acc, check_spambot=True):
        return chk

    async def ok(*a, **k):
        return True

    monkeypatch.setattr("services.account_manager.check_account_status_full", fake_check)
    monkeypatch.setattr("services.account_status.set_status", ok)
    r = {"acc_id": 701, "owner_id": 7, "phase": "recheck", "session_str": "x" * 20,
         "attempts": 0, "appeal_count": 1, "warm_cycles": 2, "kind": "temp"}
    r.update(row)
    pool = _Pool()
    _run(ar._process(pool, r))
    return pool


def test_known_release_date_keeps_account_out_of_stuck(monkeypatch):
    rel = datetime.now(timezone.utc) + timedelta(days=20)
    reply = f"Your account will be automatically released on {rel:%d %b %Y}, 10:00 UTC."
    pool = _recheck(monkeypatch, {"status": "spamblock", "spamblock_kind": "temp",
                                  "spambot_reply": reply},
                    attempts=ar._MAX_RECHECKS + 2)
    args = pool.advance()
    assert args[1] == "warming", "блок с названной датой отдан «на разбор» до даты"
    assert args[10] is not None, "дата снятия не сохранена"
    assert "release_date" in pool.steps()


def test_without_release_date_limit_still_applies(monkeypatch):
    pool = _recheck(monkeypatch, {"status": "spamblock", "spamblock_kind": "temp"},
                    attempts=ar._MAX_RECHECKS)
    assert pool.advance()[1] == "stuck"


def test_owner_limit_of_rechecks_is_respected(monkeypatch):
    pool = _recheck(monkeypatch, {"status": "spamblock", "spamblock_kind": "temp"},
                    attempts=3, _cfg={"max_rechecks": 4})
    assert pool.advance()[1] == "stuck"


def test_hard_cap_even_when_waiting_for_date(monkeypatch):
    rel = datetime.now(timezone.utc) + timedelta(days=20)
    pool = _recheck(monkeypatch, {"status": "spamblock"}, release_at=rel,
                    attempts=ar._HARD_MAX_RECHECKS)
    assert pool.advance()[1] == "stuck"


def test_warming_waits_for_far_release_date_quietly(monkeypatch):
    async def warm(p, a):
        return 3
    monkeypatch.setattr(ar, "_quiet_warmup", warm)
    rel = datetime.now(timezone.utc) + timedelta(days=10)
    pool = _Pool()
    _run(ar._process(pool, {"acc_id": 702, "owner_id": 7, "phase": "warming",
                            "session_str": "x" * 20, "attempts": 1, "appeal_count": 1,
                            "warm_cycles": 5, "kind": "temp", "release_at": rel}))
    args = pool.advance()
    assert args[1] == "warming", "до даты снятия перепроверять незачем"
    assert args[8] >= 15 * 3600, "прогрев до даты должен быть редким (раз в сутки)"


def test_warming_schedules_recheck_right_after_near_release(monkeypatch):
    async def warm(p, a):
        return 3
    monkeypatch.setattr(ar, "_quiet_warmup", warm)
    rel = datetime.now(timezone.utc) + timedelta(hours=5)
    pool = _Pool()
    _run(ar._process(pool, {"acc_id": 703, "owner_id": 7, "phase": "warming",
                            "session_str": "x" * 20, "attempts": 1, "appeal_count": 1,
                            "warm_cycles": 0, "kind": "temp", "release_at": rel}))
    args = pool.advance()
    assert args[1] == "recheck"
    assert 5 * 3600 <= args[8] <= 5 * 3600 + 2 * 3600, "перепроверка не сразу после даты"


# ── Отдых после снятия ───────────────────────────────────────────────────────
def test_freed_account_rests_before_operations(monkeypatch):
    pool = _recheck(monkeypatch, {"status": "active"})
    rest = [a for sql, a in pool.execs if "cooldown_until" in sql]
    assert rest, "снятый аккаунт сразу ушёл в операции"
    assert rest[0][1] == 24
    assert "freed" in pool.steps() and "rest" in pool.steps()


def test_rest_can_be_turned_off(monkeypatch):
    pool = _recheck(monkeypatch, {"status": "active"}, _cfg={"rest_hours": 0})
    assert not [a for sql, a in pool.execs if "cooldown_until" in sql]


# ── Аппеляция по настройке ───────────────────────────────────────────────────
def test_auto_appeal_off_skips_spambot(monkeypatch):
    called = []

    async def appeal(*a, **k):
        called.append(1)
        return {"status": "appeal_sent"}
    monkeypatch.setattr("services.account_manager.appeal_spamblock", appeal)
    pool = _Pool()
    _run(ar._process(pool, {"acc_id": 704, "owner_id": 7, "phase": "appeal",
                            "session_str": "x" * 20, "attempts": 0, "appeal_count": 0,
                            "warm_cycles": 0, "_cfg": {"auto_appeal": False}}))
    assert not called, "автоматика написала @SpamBot вопреки настройке"
    assert pool.advance()[1] == "warming"
    assert "appeal_skipped" in pool.steps()


def test_appeal_reads_release_date_from_full_reply(monkeypatch):
    rel = datetime.now(timezone.utc) + timedelta(days=9)

    async def appeal(*a, **k):
        return {"status": "still_blocked", "reply": "Unfortunately…",
                "full_reply": f"Unfortunately… released on {rel:%d %b %Y}, 09:00 UTC."}
    monkeypatch.setattr("services.account_manager.appeal_spamblock", appeal)
    pool = _Pool()
    _run(ar._process(pool, {"acc_id": 705, "owner_id": 7, "phase": "appeal",
                            "session_str": "x" * 20, "attempts": 0, "appeal_count": 0,
                            "warm_cycles": 0}))
    args = pool.advance()
    assert args[3] == "temp" and args[10] is not None


# ── Настройки ────────────────────────────────────────────────────────────────
def test_settings_are_clamped():
    s = ar.normalize_settings({"pace": "warp", "max_rechecks": 999, "rest_hours": -5,
                               "paused": "true", "auto_appeal": 0})
    assert s["pace"] == "normal"
    assert s["max_rechecks"] == 16 and s["rest_hours"] == 0
    assert s["paused"] is True and s["auto_appeal"] is False


def test_fast_pace_never_rechecks_too_often():
    for a in range(8):
        assert ar._backoff_sec(a, "fast") >= 3 * 3600
    assert ar._backoff_sec(7, "careful") > ar._backoff_sec(7, "fast")


def test_paused_owner_rows_are_not_due():
    src = ar._due_rows.__code__.co_consts
    assert any("rehab_settings" in str(c) and "paused" in str(c) for c in src)


def test_disabled_notification_is_not_sent_but_marked(monkeypatch):
    sent = []

    class P(_Pool):
        async def fetch(self, sql, *a):
            return [{"acc_id": 1, "owner_id": 7, "phase": "freed", "attempts": 2,
                     "phone": "+7", "first_name": "Иван", "acc_status": "active"}]

    async def notify(*a, **k):
        sent.append(a)
    monkeypatch.setattr("database.db.notify_if_enabled", notify)
    pool = P(settings={"notify_freed": False})
    _run(ar.notify_terminal(pool, object()))
    assert not sent, "выключенное уведомление отправлено"
    assert any("notified_phase" in sql for sql, _ in pool.execs), "итог не помечен"


# ── Описание для экрана ──────────────────────────────────────────────────────
def test_describe_tells_release_date_and_rest():
    d = ar.describe({"phase": "warming", "release_at": NOW + timedelta(days=3)}, NOW)
    assert d["release_in_seconds"] == 3 * 86400 and d["release_at"]
    d = ar.describe({"phase": "freed", "cooldown_until": NOW + timedelta(hours=5)}, NOW)
    assert d["rest_in_seconds"] == 5 * 3600 and "отдыхает" in d["hint"]
    d = ar.describe({"phase": "stuck"}, NOW, settings={"max_rechecks": 5})
    assert d["max_attempts"] == 5


def test_every_logged_step_has_a_russian_label():
    import pathlib
    import re as _re
    src = pathlib.Path(ar.__file__).read_text(encoding="utf-8")
    steps = set(_re.findall(r'_log_event\([^)]*?,\s*"([a-z_]+)"', src, _re.S))
    steps |= {"enrolled", "relapse"}
    missing = [s for s in steps if s not in ar.EVENT_LABEL]
    assert not missing, missing

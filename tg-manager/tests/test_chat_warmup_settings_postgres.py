"""Разогрев чата: настройки, которых не хватало, — и они реально работают.

У сессии разогрева было четыре настройки: чат, режим, темы, интенсивность.
Язык реплик был жёстко русским («Пиши ТОЛЬКО на русском»), у флота не было
ни часов работы, ни лимита реплик в сутки, ни срока, а запущенную сессию нельзя
было поправить — только остановить и завести заново, потеряв журнал ходов.

Что держит этот файл:
* настройки проверяются на сервере с русским отказом и сохраняются;
* язык и длина доходят до задания модели, доля реакций — до решения хода;
* автостоп по числу реплик и по сроку останавливает сессию;
* вне часов работы и сверх лимита на сутки ход пропускается ДО обращения к
  флоту — с причиной в журнале;
* запущенную сессию можно поправить, чужую — нельзя.

Запросы новые, поэтому вторая половина — на настоящей базе.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from services import chat_warmup as cw

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER, STRANGER = 990311, 990312


# ── Чистая часть ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,word", [
    ({"lang": "de"}, "Язык"),
    ({"active_from": 9}, "и начало, и конец"),
    ({"active_from": 9, "active_to": 9}, "совпадают"),
    ({"active_from": 25, "active_to": 3}, "от 0 до 23"),
    ({"daily_limit": -1}, "от 0"),
    ({"per_account_daily": "много"}, "целое число"),
    ({"react_pct": 90}, "от 0 до 60"),
    ({"stop_after_days": 365}, "от 0 до 90"),
    ({"reply_len": "huge"}, "Длина"),
])
def test_bad_settings_are_refused_in_russian(body, word):
    with pytest.raises(ValueError) as e:
        cw.clean_settings(body)
    assert word in str(e.value)


def test_only_sent_keys_are_changed():
    assert cw.clean_settings({}) == {}
    assert cw.clean_settings({"lang": "uk"}) == {"lang": "uk"}
    assert cw.clean_settings({"active_from": None, "active_to": None}) == {
        "active_from": None, "active_to": None}


def test_language_and_length_reach_the_model():
    sys_uk, _ = cw.build_dialogue_prompt("", [], None, "", "mixed", lang="uk", reply_len="medium")
    assert "украинском" in sys_uk and "2–4 фразы" in sys_uk and "русском" not in sys_uk
    sys_auto, _ = cw.build_dialogue_prompt("", [], None, "", "mixed", lang="auto")
    assert "на котором пишут в чате" in sys_auto
    sys_def, _ = cw.build_dialogue_prompt("", [], None, "", "mixed")
    assert "ТОЛЬКО на русском" in sys_def, "без настройки поведение прежнее"


def test_reaction_share_follows_the_setting():
    assert cw.should_react("reply", True, roll=0.1, chance=0.0) is False
    assert cw.should_react("reply", True, roll=0.4, chance=0.45) is True
    assert cw.should_react("reply", True, roll=0.2) is True  # без настройки — прежние 28%


def test_work_hours_and_autostop():
    noon = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)  # 12:00 по Москве
    assert cw.in_work_hours({"active_from": 10, "active_to": 18}, noon, "Europe/Moscow")
    assert not cw.in_work_hours({"active_from": 20, "active_to": 8}, noon, "Europe/Moscow")
    assert cw.in_work_hours({}, noon, None), "без окна — всегда"
    assert cw.stop_reason({"max_messages": 5, "messages_sent": 5}, noon)
    assert cw.stop_reason({"ends_at": noon - timedelta(minutes=1)}, noon)
    assert cw.stop_reason({"max_messages": 5, "messages_sent": 4,
                           "ends_at": noon + timedelta(days=1)}, noon) is None


# ── Живая база ─────────────────────────────────────────────────────────────

def _run(body):
    async def go():
        import asyncpg
        pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
        try:
            for o in (OWNER, STRANGER):
                await pool.execute("DELETE FROM chat_warmup_log WHERE owner_id=$1", o)
                await pool.execute("DELETE FROM chat_warmup_sessions WHERE owner_id=$1", o)
            return await body(pool)
        finally:
            for o in (OWNER, STRANGER):
                await pool.execute("DELETE FROM chat_warmup_log WHERE owner_id=$1", o)
                await pool.execute("DELETE FROM chat_warmup_sessions WHERE owner_id=$1", o)
            await pool.close()

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


live = pytest.mark.skipif(not DSN, reason="нужен живой Postgres: INFRAGRAM_TEST_DSN")


@live
def test_settings_are_saved_and_edited():
    async def body(pool):
        s = await cw.create_session(pool, OWNER, "t.me/settings_chat", [1, 2], settings=cw.clean_settings({
            "lang": "uk", "active_from": 9, "active_to": 21, "daily_limit": 40,
            "per_account_daily": 6, "reply_len": "medium", "react_pct": 15,
            "stop_after_days": 7, "max_messages": 300}))
        edited = await cw.update_settings(pool, OWNER, int(s["id"]), {
            **cw.clean_settings({"active_from": None, "active_to": None, "stop_after_days": 0}),
            "topics": "футбол", "account_ids": [2]})
        foreign = await cw.update_settings(pool, STRANGER, int(s["id"]), {"topics": "чужое"})
        return s, edited, foreign

    s, edited, foreign = _run(body)
    assert (s["lang"], s["active_from"], s["active_to"], s["daily_limit"]) == ("uk", 9, 21, 40)
    assert (s["per_account_daily"], s["reply_len"], s["react_pct"], s["max_messages"]) == (6, "medium", 15, 300)
    assert s["ends_at"] is not None and s["ends_at"] > datetime.now(timezone.utc) + timedelta(days=6)
    assert edited["active_from"] is None and edited["ends_at"] is None, "окно и срок не снялись"
    assert edited["topics"] == "футбол" and list(edited["account_ids"]) == [2]
    assert edited["lang"] == "uk", "правка задела то, чего не присылали"
    assert foreign is None, "чужую сессию удалось поправить"


async def _session(pool, **cols):
    s = await cw.create_session(pool, OWNER, "t.me/gate_chat", [1, 2])
    sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(cols))
    if sets:
        await pool.execute(f"UPDATE chat_warmup_sessions SET {sets} WHERE id=$1",
                           int(s["id"]), *cols.values())
    return dict(await pool.fetchrow("SELECT * FROM chat_warmup_sessions WHERE id=$1", int(s["id"])))


async def _last_reason(pool, sid):
    return await pool.fetchval(
        "SELECT reason FROM chat_warmup_log WHERE session_id=$1 ORDER BY id DESC LIMIT 1", sid)


@live
def test_autostop_after_n_messages_stops_the_session(monkeypatch):
    called = []

    async def _never(*a, **k):
        called.append(1)
        return {}

    monkeypatch.setattr(cw, "fleet_availability", _never)

    async def body(pool):
        s = await _session(pool, max_messages=5, messages_sent=5)
        await cw._process_session(pool, s)
        st = await pool.fetchval("SELECT status FROM chat_warmup_sessions WHERE id=$1", s["id"])
        return st, await _last_reason(pool, s["id"])

    st, reason = _run(body)
    assert st == "stopped" and "Остановить после" in reason
    assert not called, "остановленная по настройке сессия всё равно пошла к флоту"


@live
def test_out_of_hours_and_over_the_daily_limit_skip_before_the_fleet(monkeypatch):
    called = []

    async def _never(*a, **k):
        called.append(1)
        return {}

    monkeypatch.setattr(cw, "fleet_availability", _never)
    from zoneinfo import ZoneInfo
    h = datetime.now(ZoneInfo("Europe/Moscow")).hour

    async def body(pool):
        off = await _session(pool, active_from=(h + 2) % 24, active_to=(h + 3) % 24)
        await cw._process_session(pool, off)
        r_off = await _last_reason(pool, off["id"])
        full = await _session(pool, daily_limit=2)
        for _ in range(2):
            await cw._log(pool, full, "ok", kind="seed", text="привет", account_id=1)
        await cw._process_session(pool, full)
        r_full = await _last_reason(pool, full["id"])
        per = await _session(pool, per_account_daily=1)
        for acc in (1, 2):
            await cw._log(pool, per, "ok", kind="seed", text=f"реплика {acc}", account_id=acc)
        await cw._process_session(pool, per)
        r_per = await _last_reason(pool, per["id"])
        return r_off, r_full, r_per

    r_off, r_full, r_per = _run(body)
    assert r_off and "Вне часов работы" in r_off
    assert r_full and "Лимит на сутки исчерпан: 2 из 2" in r_full
    assert r_per and "лимит на аккаунт" in r_per
    assert not called, "ход вне часов или сверх лимита дошёл до флота"


def test_the_screen_offers_and_edits_them():
    from tests.miniapp_source import js_function, miniapp_html
    html = miniapp_html()
    assert "⚙️ Дополнительные настройки" in html and 'onclick="cwOpenEdit()"' in html
    form = js_function("_cwSettingsHtml")
    for field in ("Lang", "From", "To", "Len", "React", "Daily", "PerAcc", "Days", "Max"):
        assert f"p+'{field}'" in form or f"${{p}}{field}" in form, f"нет поля {field}"
    assert "/settings`" in js_function("cwSaveEdit")
    assert "_cwReadSettings('cwN')" in js_function("cwStart")

"""Замер онбординга: время до первого результата и доля дошедших по шагам.

В плане стояло «мерить время до первого результата и долю дошедших по
шагам». Момент активации писался, но начала пути и шагов не было — посчитать
было не из чего. Теперь каждый закрытый шаг пишется один раз (первое время),
отчёт виден админу в боте (/admin_onboarding) и в админке мини-аппа.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from services import onboarding as onb
from tests.miniapp_source import miniapp_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DSN = os.getenv("INFRAGRAM_TEST_DSN")


def test_steps_to_record_marks_start_done_steps_and_result():
    cl = {"steps": [{"id": "accounts", "done": True}, {"id": "proxy", "done": False},
                    {"id": "plan", "done": True}], "activated": False}
    assert onb.steps_to_record(cl) == ["start", "accounts", "plan"]
    cl["activated"] = True
    assert onb.steps_to_record(cl)[-1] == "result"


def test_report_text_is_honest_about_an_empty_window():
    txt = onb.format_activation_report({"days": 30, "started": 0, "activated": 0,
                                        "median_to_result_s": None, "steps": [], "goals": []})
    assert "Никто ещё не открывал" in txt
    txt = onb.format_activation_report({
        "days": 7, "started": 10, "activated": 4, "median_to_result_s": 5400,
        "steps": [{"id": "accounts", "title": "Подключили аккаунт", "done": 8, "share": 0.8}],
        "goals": [{"goal": "crm", "title": "📇 Вести клиентов в CRM", "started": 6}]})
    assert "Начали: <b>10</b>" in txt and "(40%)" in txt
    assert "1 ч 30 мин" in txt and "Подключили аккаунт: 8 (80%)" in txt


def test_wired_in_checklist_bot_and_admin_screen():
    src = open(os.path.join(ROOT, "services", "onboarding.py"), encoding="utf-8").read()
    body = src[src.index("async def load_checklist"):]
    body = body[:body.index("\ndef steps_to_record")]
    assert "await record_steps(pool, uid, checklist)" in body
    adm = open(os.path.join(ROOT, "bot", "handlers", "admin.py"), encoding="utf-8").read()
    assert 'Command("admin_onboarding")' in adm and "activation_report(pool, days)" in adm
    api = open(os.path.join(ROOT, "services", "mini_app_admin.py"), encoding="utf-8").read()
    assert 'stats["onboarding"] = await _onb.activation_report(pool, 30)' in api
    assert "html += _admOnboardingHtml(s.onboarding);" in miniapp_html()
    assert "schema_v311_onboarding_step_log.sql" in open(
        os.path.join(ROOT, "schema_checksums.txt"), encoding="utf-8").read()


def test_funnel_on_real_schema():
    if not DSN:
        pytest.skip("нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")
    import asyncpg
    a, b, c = 88_600_001, 88_600_002, 88_600_003

    async def _go():
        try:
            pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
        except Exception as exc:
            pytest.skip(f"Postgres недоступен: {str(exc)[:120]}")
        ids = [a, b, c]
        try:
            await pool.execute("DELETE FROM onboarding_step_log WHERE owner_id = ANY($1::bigint[])", ids)
            await pool.execute("DELETE FROM onboarding_step_log WHERE step_id='start'")
            for uid in ids:
                await onb.record_steps(pool, uid, {"goal": "crm", "steps": [], "activated": False})
            await onb.record_steps(pool, a, {"goal": "crm", "activated": True,
                                             "steps": [{"id": "contacts", "done": True}]})
            await onb.record_steps(pool, b, {"goal": "crm", "activated": False,
                                             "steps": [{"id": "contacts", "done": True}]})
            # Время — от первого открытия: сдвигаем старт «а» на два часа назад.
            await pool.execute("UPDATE onboarding_step_log SET done_at = now() - interval '2 hours' "
                               "WHERE owner_id=$1 AND step_id='start'", a)
            # Повторная запись не сдвигает первое время.
            await onb.record_steps(pool, a, {"goal": "crm", "activated": True, "steps": []})
            d = await onb.activation_report(pool, 30)
            assert d["started"] == 3 and d["activated"] == 1
            assert 7000 < d["median_to_result_s"] < 7400
            by = {s["id"]: s for s in d["steps"]}
            assert by["contacts"]["done"] == 2 and by["contacts"]["share"] == round(2 / 3, 3)
            assert d["goals"][0]["goal"] == "crm" and d["goals"][0]["started"] == 3
        finally:
            await pool.execute("DELETE FROM onboarding_step_log WHERE owner_id = ANY($1::bigint[])", ids)
            await pool.close()
    asyncio.run(_go())

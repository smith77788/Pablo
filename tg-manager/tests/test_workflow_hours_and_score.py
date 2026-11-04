"""Условие «рабочее время» и действие «прибавить к полю» (баллы лида)."""
from datetime import datetime, timezone

import pytest

from services import workflow_engine as wf
from services import workflow_runtime as rt
from tests.miniapp_source import miniapp_html

MSK = "Europe/Moscow"


def _utc(y, m, d, h):
    return datetime(y, m, d, h, 0, tzinfo=timezone.utc)


def test_work_hours_are_checked_in_the_persons_zone():
    # 2026-11-04 — среда. 07:00 UTC = 10:00 Москва.
    assert rt.in_work_hours(_utc(2026, 11, 4, 7), "09-18", MSK)
    assert not rt.in_work_hours(_utc(2026, 11, 4, 16), "09-18", MSK)  # 19:00
    assert rt.in_work_hours(_utc(2026, 11, 4, 20), "22-06", MSK)      # 23:00, через полночь


def test_weekdays_only_skips_the_weekend():
    sat_noon_msk = _utc(2026, 11, 7, 9)
    assert rt.in_work_hours(sat_noon_msk, "09-18", MSK)
    assert not rt.in_work_hours(sat_noon_msk, "09-18 будни", MSK)
    assert rt.in_work_hours(_utc(2026, 11, 6, 9), "09-18 будни", MSK)  # пятница


def test_the_condition_is_validated_and_normalised():
    s = wf.validate_step({"type": "condition", "condition": "work_hours",
                          "condition_value": "9-18 Будни"})
    assert s["condition_value"] == "09-18 будни"
    for bad in ("", "утром", "18-18"):
        with pytest.raises(ValueError):
            wf.validate_step({"type": "condition", "condition": "work_hours",
                              "condition_value": bad})


def test_add_to_field_counts_from_zero_and_keeps_numbers_clean():
    assert rt.add_to_number(None, "10") == "10"
    assert rt.add_to_number("10", "-3") == "7"
    assert rt.add_to_number("1 500", "2,5") == "1502.5"
    assert rt.add_to_number("не число", "5") == "5"


def test_add_to_field_is_validated():
    s = wf.validate_step({"type": "action", "action": "add_to_field", "value": "Score+=10"})
    assert s["value"] == "score+=10"
    assert wf.validate_step({"type": "action", "action": "add_to_field",
                             "value": "score+=-2.50"})["value"] == "score+=-2.5"
    for bad in ("score=10", "score+=много", "+=5", "score+=99999999"):
        with pytest.raises(ValueError):
            wf.validate_step({"type": "action", "action": "add_to_field", "value": bad})


def test_the_add_step_screen_offers_everything_the_engine_runs():
    """Регрессия: в окне шага не было сравнений полей и запуска других
    сценариев — движок их исполнял, а выбрать их было нельзя."""
    html = miniapp_html()
    cond = html[html.index('id="wfStepCondition"'):]
    cond = cond[:cond.index("</select>")]
    act = html[html.index('id="wfStepAction"'):]
    act = act[:act.index("</select>")]
    for key in set(wf.WORKFLOW_CONDITIONS) - {"has_tag", "event"}:
        assert f'value="{key}"' in cond, key
    for key in wf.WORKFLOW_ACTIONS:
        assert f'value="{key}"' in act, key


def test_notify_chat_target_is_validated():
    assert rt.parse_notify_chat("-1001234567 | Новая заявка") == ("-1001234567", "Новая заявка")
    assert rt.parse_notify_chat("@sales_team") == ("@sales_team", "")
    for bad in ("", "sales", "@ab", "https://t.me/x", "-12"):
        with pytest.raises(ValueError):
            rt.parse_notify_chat(bad)
    s = wf.validate_step({"type": "action", "action": "notify_chat",
                          "value": "@sales_team|  Горячий лид "})
    assert s["value"] == "@sales_team | Горячий лид"

"""/scenarios: сводка сценариев в чате и действия после оповещения о сбое.

ЧТО БЫЛО. Оповещение «сценарий упал» приходило в чат, а повторить упавшее
или поставить сценарий на паузу можно было только в мини-аппе.
"""
import os

from bot.handlers import scenarios

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _item(**kw):
    base = {"id": 7, "name": "Дожим <b>", "is_active": True, "trigger_type": "start",
            "started": 10, "completed": 6, "failed": 2, "goals": 3, "live": 2,
            "failed_week": 4}
    base.update(kw)
    return base


def test_the_summary_shows_failures_and_offers_the_retry():
    text, kb = scenarios.render([_item()])
    assert "#7 Дожим &lt;b&gt;" in text and "упало 2" in text and "🎯 3" in text
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert any(b.callback_data == "scen:retry:7" for b in buttons)
    assert any(b.callback_data == "scen:pause:7" for b in buttons)


def test_no_retry_button_without_failures_and_resume_for_paused():
    _, kb = scenarios.render([_item(failed_week=0, is_active=False)])
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "scen:retry:7" not in data and "scen:resume:7" in data


def test_the_command_is_in_the_menu_and_the_router_is_included():
    main = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert 'command="scenarios"' in main
    assert "include_router(scenarios_handler.router)" in main

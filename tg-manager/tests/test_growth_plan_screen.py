"""Экран «🎯 План роста»: цели вместо калькулятора долей.

* на экране есть список целей, форма новой цели и экран одной цели с делами
  на сегодня, рычагами и журналом действий;
* каждое действие, которое сервер может назвать у рычага или дела
  (`action.kind`), экран умеет открыть — иначе строка рычага была бы мёртвой;
* самопиар из плана открывается уже с выбранным каналом или ботом;
* удаление цели спрашивает и называет её.
"""
from __future__ import annotations

import pathlib
import re

from tests.miniapp_source import js_function, miniapp_html

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_hub_shows_goals_and_a_goal_has_its_own_screen():
    html = miniapp_html()
    for el in ('id="gpGoals"', 'id="gpNewWrap"', 'id="gpAsset"', 'id="gpTarget"', 'id="gpDays"',
               'id="s-growthgoal"', 'id="gpToday"', 'id="gpLevers"', 'id="gpLog"', 'id="gpCoverage"'):
        assert el in html, el
    assert "gpLoad()" in js_function("openGrowthHub")
    assert "/api/miniapp/growth/goals" in js_function("gpLoad")
    f = js_function("gpOpenGoal")
    assert "/plan`" in f and "p.verdict" in f and "_gpChart(g)" in f


def test_every_action_the_server_can_name_is_opened_by_the_screen():
    server = (ROOT / "services" / "growth_plan.py").read_text(encoding="utf-8")
    named = set(re.findall(r'"kind":\s*"([a-z_]+)"', server))
    named |= set(re.findall(r'"kind":\s*"[a-z_]+"\s+if\s+\w+\s+else\s+"([a-z_]+)"', server))
    assert {"self_promo", "invite", "accounts", "parser", "va", "growth_agent", "seo",
            "boost", "edit_goal", "new_goal"} <= named, named
    handled = set(re.findall(r"case '([a-z_]+)'", js_function("gpAct")))
    assert named <= handled, f"экран не умеет открыть: {sorted(named - handled)}"


def test_self_promo_opens_with_the_asset_already_chosen():
    f = js_function("gpOpenSelfPromo")
    assert "spcOpenEditor(null, {target_kind: kind, target_ref: id})" in f
    # Есть кампания по этой цели — открываем её, а не плодим вторую.
    assert "spcOpenEditor(mine.id)" in f
    assert "gpOpenSelfPromo(g.asset_kind, g.asset_id)" in js_function("gpAct")


def test_deleting_a_goal_asks_and_names_it():
    f = js_function("gpDeleteGoal")
    assert "askConfirm(" in f and "g.name" in f
    assert f.index("askConfirm(") < f.index("/delete")

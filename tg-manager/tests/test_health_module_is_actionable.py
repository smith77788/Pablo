"""Модуль «Здоровье»: итог объясняет себя, каждое число и тревога ведут к делу.

Без базы: чистая функция итога (`fleet_verdict`) и проводка экрана. Живой
путь — в `test_health_module_tells_the_truth_postgres.py`.
"""
from __future__ import annotations

import os
import re

from services.mini_app_api import ACCOUNT_FILTERS, fleet_verdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _html() -> str:
    with open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8") as f:
        return f.read()


def _js_func(name: str) -> str:
    h = _html()
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", h)
    assert m, f"функция {name} не найдена"
    depth = 0
    for j in range(m.end() - 1, len(h)):
        if h[j] == "{":
            depth += 1
        elif h[j] == "}":
            depth -= 1
            if depth == 0:
                return h[m.start():j + 1]
    raise AssertionError(name)


_HEALTHY = {"total": 10, "active": 10}


def test_healthy_fleet_is_norm():
    v = fleet_verdict(_HEALTHY, [])
    assert v["level"] == "ok" and v["reasons"] == []


def test_critical_alert_beats_a_quiet_account_list():
    """Ровно экран владельца: аккаунты в порядке, детектор кричит."""
    v = fleet_verdict(_HEALTHY, [{"severity": "critical"}])
    assert v["level"] == "crit"
    assert v["reasons"][0]["open"] == "infra"


def test_no_usable_account_is_critical_and_opens_the_funnel():
    v = fleet_verdict({"total": 5, "active": 0, "cooling": 5}, [])
    assert v["level"] == "crit"
    assert v["reasons"][0]["key"] == "none_usable"
    assert v["reasons"][0]["open"] == "fleetdoctor"


def test_each_problem_names_itself_and_its_slice():
    c = dict(_HEALTHY, cooling=2, spamblock=1, low_trust=3, flooded_24h=1,
             dead=4, proxy_down=2)
    v = fleet_verdict(c, [{"severity": "warning"}])
    assert v["level"] == "warn"
    got = {r["key"]: r for r in v["reasons"]}
    for key in ("cooldown", "spamblock", "low_trust", "flooded_24h", "dead",
                "proxy_down", "alerts_warn"):
        assert key in got, f"причина {key} не названа"
        assert got[key]["hint"], f"у причины {key} нет подсказки, что делать"
    for r in v["reasons"]:
        if r["filter"]:
            assert r["filter"] in ACCOUNT_FILTERS, (
                f"причина ведёт в срез {r['filter']}, которого список не знает — "
                f"он молча стал бы «все аккаунты»")


def test_screen_draws_the_verdict_not_a_two_signal_status():
    body = _js_func("openHealth")
    assert "healthVerdictHtml(d.verdict)" in body
    assert "d.banned>0||d.low_trust>0" not in body, "вернулся статус из двух признаков"


def test_every_tile_opens_its_list():
    body = _js_func("openHealth")
    for f in ("low_trust", "flooded", "cooldown", "spamblock", "banned", "active"):
        assert f"go('{f}'" in body, f"плитка {f} снова просто число"
    assert "plain(" not in body
    go = _js_func("healthReasonGo")
    assert "openInfraHealth()" in go and "openFleetDoctor()" in go


def test_alert_card_has_action_mute_and_resolve():
    body = _js_func("openInfraHealth")
    for bit in ("ihAnomalyAct(", "ihMute(", "ihResolve(", "ihResolveAll(", "occurrences"):
        assert bit in body, f"на карточке оповещения нет {bit}"
    assert "/infra_health/mute/" in _js_func("ihMute")
    assert "askConfirm" in _js_func("ihResolveAll")


def test_diag_tab_leads_to_the_funnel():
    h = _html()
    i = h.index('id="s-diag"')
    assert "openFleetDoctor()" in h[i:h.index("</div>\n</div>", i)]

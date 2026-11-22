"""«Защита аккаунтов» и сводка «Требует внимания»: понятные сбои и интервал
мониторинга, который не врёт.

Раньше эти экраны показывали `e.message` как есть — сетевой сбой приходил
владельцу английским «Failed to fetch». Кнопка запуска после сбоя теряла
счётчик («🛡 Проверить» вместо «🛡 Проверить 12 сущностей»), а список
интервала авто-мониторинга всегда стоял на «12 часах», даже если проверка
уже включена на сутки.
"""
import json
import re
import subprocess
from pathlib import Path

SCREENS = Path(__file__).resolve().parents[1] / "mini_app/screens"
SHIELD = (SCREENS / "shield_check.js").read_text(encoding="utf-8")
DASH = (SCREENS / "dashboard.js").read_text(encoding="utf-8")

# Экраны, где сырой e.message ещё живёт. Список только сокращается: новый
# экран с `(e && e.message) ||` краснит этот тест.
RAW_MESSAGE_LEFT: set = set()


def _fn(src, name):
    one = re.search(r"\nfunction " + name + r"\(.*\}\n", src)
    if one and one.group(0).count("{") == one.group(0).count("}"):
        return one.group(0)
    m = re.search(r"\n(?:async )?function " + name + r"\([\s\S]*?\n}\n", src)
    assert m, name
    return m.group(0)


def _run(js):
    out = subprocess.run(["node", "-e", js], check=False, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_no_raw_error_message_in_toasts():
    for name in ("shield_check.js", "dashboard.js", "editorial.js"):
        src = (SCREENS / name).read_text(encoding="utf-8")
        assert "e.message" not in src, name


def test_raw_error_message_does_not_spread():
    offenders = {
        p.name for p in SCREENS.glob("*.js")
        if re.search(r"\(e && e\.message\) \|\|", p.read_text(encoding="utf-8"))
    }
    assert offenders <= RAW_MESSAGE_LEFT, offenders - RAW_MESSAGE_LEFT


def test_run_buttons_restore_their_own_label():
    for fn in ("runShieldCheck", "runBotGeoban"):
        body = _fn(SHIELD, fn)
        assert "const label = btn ? btn.textContent : ''" in body, fn
        assert "btn.textContent = label;" in body, fn


def _initial(monitors, saved):
    js = (_fn(SHIELD, "_shMonInitialInterval")
          + "console.log(JSON.stringify(_shMonInitialInterval(%s, %s)))"
          % (json.dumps(monitors), json.dumps(saved)))
    return _run(js)


def test_interval_follows_enabled_monitor_then_saved_choice():
    assert _initial({"health": {"enabled": True, "interval_min": 1440}}, 4320) == 1440
    assert _initial({"health": {"enabled": False, "interval_min": 1440}}, 4320) == 4320
    assert _initial({}, None) == 720
    assert _initial(None, None) == 720


def test_unknown_interval_is_kept_in_the_list():
    js = (
        "function esc(s){return String(s)}\n"
        + SHIELD[SHIELD.index("const _SH_MON_INTERVALS"):SHIELD.index("function _shMonSavedInterval")]
        + _fn(SHIELD, "_shHourPlural") + _fn(SHIELD, "_shDayPlural")
        + _fn(SHIELD, "_shMonFmtInterval") + _fn(SHIELD, "_shMonIntervalOptions")
        + "console.log(JSON.stringify([_shMonIntervalOptions(1440), _shMonIntervalOptions(180)]))"
    )
    known, custom = _run(js)
    assert '<option value="1440" selected>' in known
    assert known.count("selected") == 1
    assert '<option value="180" selected>каждые 3 часа</option>' in custom


def test_choice_is_remembered_and_used():
    assert "_shMonIntervalOptions(_shMonInitialInterval(m, _shMonSavedInterval()))" in SHIELD
    assert "saveFormPrefs('shield_monitor', { interval_min: interval })" in _fn(SHIELD, "shMonToggle")


def test_dashboard_unknown_screen_is_not_silent():
    assert "else toast('⚠️ Раздел недоступен')" in _fn(DASH, "_udAct")


def test_analytics_period_is_chosen_and_remembered():
    load = _fn(DASH, "_udLoad")
    assert "api('/api/miniapp/dashboard_realtime?range=' + rng)" in load
    assert "_udRangeRow(rng) + _udRenderAnalytics(d, rng)" in load
    assert "saveFormPrefs('unified_dash', { range: rng })" in _fn(DASH, "udSetRange")
    js = ("const PREFS={};function loadFormPrefs(k){return PREFS[k]||{}}"
          + _fn(DASH, "_udRange").replace("function _udRange", "const _UD_RANGES = [['7d', 7], ['30d', 30], ['90d', 90]];\nfunction _udRange")
          + "const a=_udRange(); PREFS.unified_dash={range:'30d'}; const b=_udRange();"
          + "PREFS.unified_dash={range:'1y'}; console.log(JSON.stringify([a,b,_udRange()]))")
    assert _run(js) == ["7d", "30d", "7d"]


def test_dashboard_error_can_be_retried():
    assert "onclick=\"_udReload()\"" in _fn(DASH, "_udError")


def test_analytics_empty_states_explain_why():
    assert "Нет данных" not in DASH
    assert "Пока мало точек для графика" in DASH
    assert "Каналов пока нет." in DASH and "openChannels()" in DASH
    assert "За период событий не было" in DASH

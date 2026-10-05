"""Карта присутствия: страна и пробел — не числа, а переходы.

Экран «🗺️ Карта присутствия» показывал четыре плитки-числа, столбики с сырыми
кодами стран («DE», «NL») и список пробелов с английскими названиями из
генератора («Germany»). Кликнуть было нельзя ничего: ни посмотреть, что за
аккаунты в этой стране, ни открыть план, из которого растёт пробел. Ошибка
падала только в средний блок — два других оставались пустыми, и это выглядело
как «данных нет».

Тест держит рабочее состояние и, главное, честность среза: фильтр по стране
обязан доехать и до «применить ко всему срезу», иначе массовая операция уйдёт
по всему флоту, а владелец видел одну страну.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()

from services import presence_map  # noqa: E402


def _fn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"функция {name} не найдена"
    i = HTML.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[i:j + 1]
    raise AssertionError(name)


# ── русские названия стран ───────────────────────────────────────────────────

def test_country_ru_gives_a_russian_name_and_a_flag():
    de = presence_map.country_ru("de")
    assert de["name"] == "Германия"
    assert de["flag"] == "🇩🇪"
    assert de["title"] == "🇩🇪 Германия"


def test_country_ru_handles_unknown_and_junk():
    u = presence_map.country_ru("UNKNOWN")
    assert u["name"] == "Без гео-прокси"
    # неизвестный код — подпись всё равно есть, а не пустая строка
    zz = presence_map.country_ru("ZZ", "Зазеркалье")
    assert zz["name"] == "Зазеркалье"
    assert presence_map.country_ru("")["code"] == "UNKNOWN"


def test_summarize_targets_carries_russian_names_and_plans():
    rows = [
        {"country": "Germany", "country_code": "de", "status": "pending",
         "plan_id": 7, "plan_name": "DE-каналы"},
        {"country": "Germany", "country_code": "de", "status": "done",
         "plan_id": 7, "plan_name": "DE-каналы"},
    ]
    de = presence_map.summarize_targets(rows)["countries"]["DE"]
    assert de["name_ru"] == "Германия", "страна осталась с английским именем из плана"
    assert de["plans"] == [{"id": 7, "name": "DE-каналы"}], \
        "пробел не знает своего плана — открыть его нечем"


def test_summarize_targets_without_plan_id_still_works():
    de = presence_map.summarize_targets(
        [{"country": "Germany", "country_code": "de", "status": "pending"}]
    )["countries"]["DE"]
    assert de["plans"] == []


# ── срез по стране ───────────────────────────────────────────────────────────

def test_accounts_where_supports_a_geo_slice():
    assert re.search(r"def _accounts_where\([^)]*geo: str = \"\"", API, re.S), \
        "у выборки аккаунтов нет среза по стране"
    assert "geo_country" in API


def test_accounts_list_reads_the_geo_param():
    m = re.search(r"async def accounts\(request.*?where, args = _accounts_where\((.*?)\)",
                  API, re.S)
    assert m, "не нашёл сборку WHERE в списке аккаунтов"
    assert "geo=" in m.group(1), "список аккаунтов игнорирует ?geo="
    assert '_geo_code(qs.get("geo"))' in API


def test_select_all_filtered_respects_the_geo_slice():
    """Иначе масс-операция уйдёт шире, чем владелец видел на экране."""
    m = re.search(r'if body\.get\("select_all_filtered"\):(.*?)owned = await',
                  API, re.S)
    assert m, "не нашёл ветку «весь срез»"
    assert '_geo_code(body.get("geo"))' in m.group(1), \
        "«весь срез» не знает про страну — операция уйдёт по всему флоту"
    assert "geo=bgeo" in m.group(1)


def test_geo_code_validator_rejects_junk():
    ns = {}
    src = re.search(r"def _geo_code\(raw\).*?\n\n\n", API, re.S).group(0)
    exec(src, ns)
    f = ns["_geo_code"]
    assert f("de") == "DE"
    assert f("UNKNOWN") == "UNKNOWN"
    assert f("DE'; DROP TABLE") == ""
    assert f("") == "" and f(None) == ""
    assert f("DEU") == ""


def test_front_sends_and_shows_the_geo_slice():
    q = _fn("_accQuery")
    assert "p.set('geo', ACC_GEO_FILTER)" in q, "срез по стране не уходит на сервер"
    sel = _fn("_massSel")
    assert "geo: ACC_GEO_FILTER" in sel, \
        "«весь срез» на фронте не передаёт страну"
    banner = _fn("accPoolBanner")
    assert "ACC_GEO_FILTER" in banner, "срез по стране ничем не показан"
    assert "function accGeoClear(" in HTML, "срез по стране нельзя снять"


def test_switching_slices_drops_the_country():
    body = _fn("healthGoAccounts")
    assert "ACC_GEO_FILTER = ''" in body, \
        "страна остаётся при переключении среза — список молча пустеет"


# ── сам экран ────────────────────────────────────────────────────────────────

def test_country_row_opens_the_accounts_of_that_country():
    body = _fn("openPresenceMap")
    assert "pmOpenCountry(" in body, "страна на карте никуда не ведёт"
    assert "function pmOpenCountry(" in HTML
    assert "ACC_GEO_FILTER = cc" in _fn("pmOpenCountry")


def test_gap_row_opens_its_plan():
    body = _fn("openPresenceMap")
    assert "openGpDetail(" in body, "пробел присутствия не открывает план"
    assert "c.plans" in body


def test_kpi_chips_lead_somewhere():
    body = _fn("openPresenceMap")
    chips = re.findall(r'class="kpi-chip"([^>]*)>', body)
    assert len(chips) == 4
    assert all("onclick" in c for c in chips), "плитка-число без перехода"


def test_error_clears_every_block():
    body = _fn("openPresenceMap")
    tail = body[body.rindex("} catch"):]
    for cid in ("pmTotals", "pmGeo", "pmGaps"):
        assert cid in tail, f"при ошибке блок {cid} остаётся со старым содержимым"


def test_empty_gaps_state_has_an_action():
    body = _fn("openPresenceMap")
    assert "openGlobalPresence()" in body, "«Пробелов нет» — тупик без действия"


def test_map_backend_returns_russian_countries():
    m = re.search(r"async def presence_map_overview.*?return _json_resp", API, re.S)
    assert m
    src = m.group(0)
    assert "presence_map.country_ru" in src, "страны флота уходят на экран кодами"
    assert "p.id AS plan_id" in src, "пробелы приходят без плана"

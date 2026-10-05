"""Активность подписчиков: из группы можно написать, а не только посмотреть.

Экран показывал четыре карточки — «🔥 Горячие 142 · 38%» — и больше ничего.
Кто эти сто сорок два, узнать было нельзя, написать им тоже. При этом движок
рассылок ровно эти четыре когорты умеет с самого начала: target_type=cohort и
те же ключи hot|warm|cold|lost. Экран и движок просто не были связаны.

Главное, что держит этот тест, — совпадение границ. Карточка «заходили за
неделю» и когорта, по которой уйдёт рассылка, обязаны считаться одинаково;
разойдясь, владелец целился бы по одному числу, а письмо уходило бы другим
людям.

Отдельно — источник. Счётчики экрана умеют запасной путь по bot_users, а
кампания по когорте ВСЕГДА считает по user_activity. Когда сработал запасной
путь, экран показал бы «🔥 142», а рассылка нашла бы ноль, и выглядело бы это
как поломка рассылки.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()


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


def _handler(name: str) -> str:
    i = API.index(f"async def {name}(request")
    j = API.index("\n    async def ", i + 10)
    return API[i:j]


_INTERVALS = {
    "hot": ["1 day"],
    "warm": ["7 days", "1 day"],
    "cold": ["30 days", "7 days"],
    "lost": ["30 days"],
}


def test_detector_finds_both_definitions():
    """Самопроверка: обе границы когорт в коде есть и читаются."""
    assert '_COHORT_SQL = {' in API
    assert "AS hot," in _handler("bot_engagement")


def test_cohort_boundaries_match_the_campaign():
    """Границы групп на экране и в рассылке — одни и те же."""
    i = API.index("_COHORT_SQL = {")
    cohort = API[i:API.index("}", i)]
    screen = _handler("bot_engagement")
    for key, needles in _INTERVALS.items():
        line = re.search(rf'"{key}":\s*"([^"]+)"', cohort)
        assert line, f"у рассылки пропала когорта {key}"
        for n in needles:
            assert n in line.group(1), (
                f"граница когорты {key} в рассылке больше не содержит {n}")
            assert n in screen, (
                f"граница группы {key} на экране больше не содержит {n} — "
                "карточка и рассылка посчитают разных людей")


def test_source_is_reported():
    h = _handler("bot_engagement")
    assert '"source"' in h or 'out["source"]' in h, "источник чисел не отдаётся"
    assert "cohort_ready" in h, (
        "экран не узнает, что рассылка по когорте сейчас найдёт ноль")


def test_cards_lead_to_a_campaign():
    f = _fn("loadEngagement")
    assert "startCampaignFromCohort(" in f, (
        "карточка группы снова ни на что не нажимается")
    assert "Написать им" in f, "непонятно, что карточка нажимается"
    assert "d.cohort_ready" in f, (
        "кнопка обещает рассылку даже когда она найдёт ноль получателей")


def test_bridge_fills_bot_and_cohort():
    f = _fn("startCampaignFromCohort")
    assert "cmpTargetType" in f and "'cohort'" in f
    assert "ENG_BOT_ID" in f, "бот в рассылке придётся выбирать заново"
    assert "cmpCohort" in f, "когорта в рассылке придётся выбирать заново"


def test_segment_labels_are_russian_everywhere():
    # Границы берём по самим конструкциям, а не окном фиксированной длины:
    # сдвинется код — окно промахнётся, и отрицательная проверка ниже станет
    # правдой сама по себе (храповик test_no_silently_disabled_guards).
    i = HTML.index("const ENG_SEGS")
    block = HTML[i:HTML.index("];", i)]
    for ru in ("Горячие", "Тёплые", "Остывшие", "Ушедшие"):
        assert ru in block, f"пропала подпись группы «{ru}»"
    j = HTML.index('<select id="cmpCohort">')
    sel = HTML[j:HTML.index("</select>", j)]
    assert sel.count("<option") == 4, f"в выборе когорты не четыре варианта: {sel}"
    for en in ("Hot —", "Warm —", "Cold —", "Lost —"):
        assert en not in sel, f"в выборе когорты вернулась английская подпись «{en}»"
    # Подпись под полем лежит уже за </select> — берём её отдельным куском до
    # конца её собственного div, а не «сколько-то символов дальше».
    k = HTML.index('class="field-note"', j)
    note = HTML[k:HTML.index("</div>", k)]
    assert "last_seen" not in note, "в подписи поля осталось имя колонки базы"
    assert "последнему визиту" in note, "подпись поля перестала объяснять, что считается"


def test_states_have_a_way_out():
    f = _fn("loadEngagement")
    assert "errHtml(errRu(e), 'loadEngagement()')" in f, "ошибка без повтора"
    assert "ещё нет подписчиков" in f, "пустой экран ничего не объясняет"

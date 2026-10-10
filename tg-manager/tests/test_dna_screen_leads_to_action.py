"""ДНК аудитории: факты ведут к действию, а советы не врут.

Экран «🧬 ДНК аудитории» был стеной фактов: пик активности, риск оттока,
вовлечённость, темы — и ни одной кнопки. Советы движка приходят с HTML-
разметкой (их шлёт бот с parse_mode), а экран экранировал всё подряд, и
владелец читал «<b>Лучшее время публикации:</b>» прямо в тексте.

Хуже того, один совет вёл на команду /track_post, которой в проекте нет
вообще: таблица content_performance не заполняется ничем
(track_content_performance не вызывается ниоткуда), поэтому «лучшие форматы»
всегда пусты. Совет заменён честной формулировкой.

И главное: совет «запустите реактивационную рассылку» нельзя было выполнить —
сегмента «спящие» в рассылках не существовало. Он добавлен, и тест держит
паритет: словарь сегментов живёт в ЧЕТЫРЁХ местах, и ключ, забытый в одном из
них, означает, что экран предлагает сегмент, который операция молча
проигнорирует.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = open(os.path.join(ROOT, "mini_app", "index.html"), encoding="utf-8").read()


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


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


SEG_FILES = {
    "services/mini_app_api.py": 2,      # отправка рассылки + предпросмотр получателей
    "services/broadcaster.py": 1,
    "services/op_worker.py": 1,
}


def test_segment_maps_stay_in_sync():
    """Один и тот же словарь сегментов продублирован — ключи обязаны совпадать."""
    keysets = []
    for rel, expected_copies in SEG_FILES.items():
        src = _src(rel)
        blocks = re.findall(r'\{\s*\n\s*"active_7d":.*?\n\s*\}', src, re.S)
        assert len(blocks) == expected_copies, \
            f"{rel}: копий словаря сегментов {len(blocks)}, ожидалось {expected_copies}"
        for b in blocks:
            keysets.append((rel, frozenset(re.findall(r'"(\w+)":\s*" AND', b))))
    assert keysets, "не нашёл ни одного словаря сегментов"
    first = keysets[0][1]
    for rel, ks in keysets:
        assert ks == first, (
            f"{rel}: набор сегментов разошёлся — экран предложит сегмент, "
            f"который операция проигнорирует: {sorted(ks ^ first)}")


def test_sleeping_segment_exists_everywhere():
    for rel in SEG_FILES:
        assert "inactive_14d" in _src(rel), f"{rel}: нет сегмента спящих"
    assert 'value="inactive_14d"' in HTML, "сегмент спящих нельзя выбрать в форме"


def test_sleeping_segment_catches_users_who_never_came_back():
    """last_seen IS NULL — это тоже спящий, иначе половина аудитории выпадет."""
    sql = re.search(r'"inactive_14d": "([^"]+)"', _src("services/broadcaster.py")).group(1)
    assert "last_seen IS NULL" in sql
    assert "14 days" in sql


def test_no_recommendation_points_at_a_command_that_does_not_exist():
    """В комментарии про неё написать можно, в тексте для владельца — нет."""
    code = "\n".join(ln for ln in _src("services/audience_dna.py").splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "/track_post" not in code, "совет ведёт на несуществующую команду"
    assert "track_post" not in HTML


def test_recommendations_are_not_shown_as_raw_tags():
    assert "function _dnaRecHtml(" in HTML, "советы рендерятся сырым esc()"
    body = _fn("_dnaRecHtml")
    assert "esc(" in body, "разметка вставляется без экранирования"
    assert "&lt;(\\/?b)&gt;" in body or "&lt;(\\\\/?b)&gt;" in body, \
        "возвращается не только <b>"
    prof = _fn("_renderDnaProfile")
    assert "_dnaRecHtml(r)" in prof
    assert "${esc(r)}" not in prof


def test_peak_hour_schedules_a_broadcast():
    prof = _fn("_renderDnaProfile")
    assert "dnaBcastAtPeak(" in prof, "пик активности никуда не ведёт"
    f = _fn("dnaBcastAtPeak")
    assert "openBcastForBot(" in f
    assert "bcastSchedule" in f, "час пика не попадает в расписание рассылки"
    assert "while (mins <= 0) mins += 1440;" in f, \
        "прошедший сегодня час дал бы отрицательную задержку"


def test_churn_opens_a_reactivation_broadcast():
    prof = _fn("_renderDnaProfile")
    assert "dnaReactivate()" in prof, "риск оттока — просто число"
    f = _fn("dnaReactivate")
    assert "inactive_14d" in f, "реактивация уходит не тому сегменту"
    assert "updateBcastRecip()" in f, "счётчик получателей останется от прошлого сегмента"


def test_empty_profile_offers_to_compute():
    prof = _fn("_renderDnaProfile")
    assert "recomputeDna()" in prof, "«не вычислено» без кнопки — тупик"


def test_dna_errors_offer_a_retry():
    for name in ("openDnaDetail", "recomputeDna", "openAudienceDna"):
        body = _fn(name)
        assert re.search(r"errHtml\(errRu\(e\)\s*,", body), f"{name}: ошибка без «Повторить»"

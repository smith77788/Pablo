"""Содержимое в обёртке с id держит тот же кант 16px, что и тело экрана.

Правила отступа от края (`.sb > button`, `.sb > div:not([class]):not([id])`…)
смотрят только на ПРЯМЫХ детей тела экрана. Вкладки «Аккаунты» и «Боты»
завернули инструменты в `#accTools`/`#botTools` (прятать на пустом списке) —
и кнопки «Проверить все», «Поднять флот», поиск снова легли от края до края
под карточками с отступом. Тело экранов, которые строит JS (интеграции,
продажи, защита от накрутки), — тоже обёртка с id. Обёртка с классом
`.sb-pass` для правил отступа прозрачна; здесь проверяется, что класс стоит на
каждой такой обёртке и что правила для него есть.

Рядом второй дефект той же природы: колонка действий строки списка
(`.li-right`) ужималась уже собственной кнопки, и кнопка вылезала влево поверх
названия («Объединить» в дублях контактов, «Выключить» в умных тегах).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "mini_app"
HTML = (APP / "index.html").read_text(encoding="utf-8")

WRAPPERS = ["accTools", "botTools", "fpplBody", "audsegBody", "notaryCert",
            "webhookBody", "dmrBody", "expdetailBody"]
JS_BODIES = ["flood_guard", "integrations", "sales_manager"]


def test_wrappers_are_transparent_for_edge_rules():
    for w in WRAPPERS:
        m = re.search(r'<div id="%s"([^>]*)>' % w, HTML)
        assert m, w
        assert 'class="sb-pass"' in m.group(1), w
    for f in JS_BODIES:
        src = (APP / "screens" / (f + ".js")).read_text(encoding="utf-8")
        assert """-body" class="sb-pass\"""" in src, f


def test_sb_pass_rules_pad_buttons_fields_and_blocks():
    for sel in (".sb-pass > button", ".sb-pass > select", ".sb-pass > textarea",
                ".sb-pass > div:not([class]):not([id])", ".sb-pass > .row",
                ".sb-pass > .field"):
        assert sel in HTML, sel
    m = re.search(r"\.sb-pass > button,\.sb-pass > input[^{]*\{([^}]*)\}", HTML)
    assert m and "margin-left:16px !important" in m.group(1)


def test_action_column_never_narrower_than_its_button():
    assert ".li-body + .li-right:not(.col){min-width:min-content}" in HTML

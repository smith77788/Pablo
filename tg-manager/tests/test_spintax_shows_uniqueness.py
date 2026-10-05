"""Спинтакс: сказано, сколько разных сообщений даёт шаблон.

Ради этого числа спинтакс в продукте и существует: одинаковый текст,
разосланный сотне людей, Telegram ловит как спам. Экран его не показывал —
шаблон с двумя группами по два слова (четыре сообщения на всю рассылку)
выглядел так же солидно, как шаблон на тысячу комбинаций.

Остальное на том же экране: пустой ответ оставлял белый экран без единого
слова (движок мог отбраковать всё, что вернула модель, и это выглядело как
поломка); сбой показывался текстом e.message, то есть по-английски; шаблоны
копировались строго по одному, хотя в рассылку их вставляют списком.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = open(os.path.join(ROOT, "mini_app", "screens", "spintax.js"),
          encoding="utf-8").read()
API = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
SVC = open(os.path.join(ROOT, "services", "spintax_service.py"),
           encoding="utf-8").read()


def _jsfn(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", JS)
    assert m, f"функция {name} не найдена"
    i = JS.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(JS)):
        if JS[j] == "{":
            depth += 1
        elif JS[j] == "}":
            depth -= 1
            if depth == 0:
                return JS[i:j + 1]
    raise AssertionError(name)


def test_counter_exists_in_the_engine():
    """Самопроверка: считает не экран, а движок, и функция на месте."""
    assert "def count_group_variants(" in SVC


def test_counting_is_right_on_a_known_template():
    """Измеритель на заведомо известном примере: 3·2·2·2 = 24."""
    import sys
    sys.path.insert(0, ROOT)
    from services import spintax_service as ss
    tpl = "{а|б|в} {г|д} {е|ж} {з|и}"
    groups = ss.count_group_variants(tpl)
    combos = 1
    for g in groups:
        combos *= g
    assert groups == [3, 2, 2, 2] and combos == 24, (groups, combos)


def test_api_sends_the_number():
    i = API.index("def _spintax_pack(")
    block = API[i:API.index("\ndef ", i + 10)]
    assert "count_group_variants" in block, "число комбинаций не считается"
    assert '"combos"' in block and '"groups"' in block
    assert "10 ** 9" in block, (
        "нет потолка: длинный шаблон выдаст число, которое ничего не значит")


def test_screen_shows_the_number_and_warns_when_it_is_small():
    f = _jsfn("_spinRender")
    assert "v.combos" in f, "число разных сообщений не выводится"
    assert "Telegram ловит повторы" in f, (
        "слабый шаблон не отмечен — его разошлют и получат спам-блок")
    assert "combos_capped" in f, "потолок показывается как точное число"


def test_empty_result_explains_itself():
    f = _jsfn("_spinRender")
    assert "Вариантов не вышло" in f, (
        "пустой ответ снова оставляет белый экран")
    assert "variants.length" in f


def _code_only(src: str) -> str:
    """Без строк комментариев: в них слово e.message стоит законно."""
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("//"))


def test_errors_are_russian():
    for name in ("submitSpin", "rerollSpin"):
        f = _code_only(_jsfn(name))
        assert "errRu(" in f, f"{name} показывает сырой текст ошибки"
        assert "e.message" not in f, f"{name} снова показывает e.message"


def test_all_templates_can_be_copied_at_once():
    assert "function copyAllSpinTpl" in JS, (
        "шаблоны копируются только по одному")
    f = _jsfn("copyAllSpinTpl")
    assert "SPIN_TEMPLATES.join(" in f
    assert "copyAllSpinTpl()" in _jsfn("_spinRender"), "кнопки «скопировать все» нет"


def test_tappable_template_says_it_is_tappable():
    f = _jsfn("_spinRender")
    assert "Нажмите на шаблон" in f, (
        "блок с шаблоном кликабелен, но об этом нигде не сказано")

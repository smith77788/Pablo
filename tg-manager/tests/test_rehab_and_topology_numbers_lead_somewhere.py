"""Последние два экрана-витрины: числа восстановления и топологии ведут дальше.

«Восстановление аккаунтов» показывало три счётчика — восстанавливаются,
возвращены, нужен разбор — и ни один не открывал свой список; строка аккаунта
не открывала его карточку; ошибка рисовалась пустотой без «Повторить».

«Карта топологии» — пять чисел подряд (аккаунты, каналы, боты, связи,
подписчики) и ни одного перехода, хотя первые три — это ровно три вкладки
приложения. Два последних сводного списка не имеют, поэтому остаются числами:
кнопка, ведущая не туда, хуже её отсутствия.
"""
from __future__ import annotations

import functools
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "mini_app", "index.html")


@functools.lru_cache(maxsize=1)
def _html() -> str:
    with open(HTML, encoding="utf-8") as f:
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


def _retries(body: str, screen: str) -> bool:
    """errHtml(..., 'openX()') — первый аргумент может сам быть вызовом."""
    for m in re.finditer(r"errHtml\(", body):
        depth, j = 0, m.end() - 1
        while j < len(body):
            if body[j] == "(":
                depth += 1
            elif body[j] == ")":
                depth -= 1
                if depth == 0:
                    if f"'{screen}()'" in body[m.end():j]:
                        return True
                    break
            j += 1
    return False


def test_both_screens_still_show_those_numbers():
    """Антивакуумность."""
    reh = _js_func("openRehab")
    top = _js_func("openTopology")
    for lbl in ("Восстанавливаются", "Возвращены", "Нужен разбор"):
        assert lbl in reh, f"счётчик «{lbl}» пропал"
    for lbl in ("Аккаунтов", "Каналов", "Ботов"):
        assert lbl in top, f"счётчик «{lbl}» пропал"


def test_rehab_counters_open_their_slices():
    body = _js_func("openRehab")
    assert "healthGoAccounts('active','')" in body, "«Возвращены» никуда не ведёт"
    assert "healthGoAccounts('spamblock','')" in body, "«Нужен разбор» никуда не ведёт"


def test_rehab_row_opens_the_account():
    body = _js_func("openRehab")
    assert "openAccount(" in body, "строка восстановления не открывает аккаунт"


def test_rehab_row_buttons_still_work_separately():
    """Строка открывает аккаунт — кнопки шага не должны открывать его заодно."""
    body = _js_func("openRehab")
    # переход висит на теле строки, а не на всей строке с кнопками
    assert 'class="row-body" onclick="openAccount(' in body, (
        "переход повешен на всю строку — он перехватит нажатия ⏩/⏹/🔁"
    )
    for act in ("'now'", "'off'", "'restart'"):
        assert f"rehabAct(${{Number(a.acc_id)}},{act})" in body or f"rehabAct(${{a.acc_id}},{act})" in body, \
            f"кнопка {act} пропала"


def test_rehab_error_has_a_way_out():
    assert _retries(_js_func("openRehab"), "openRehab"), "ошибка восстановления — тупик"


def test_topology_counters_open_their_tabs():
    body = _js_func("openTopology")
    assert "healthGoAccounts('all','')" in body
    assert "goTab('channels')" in body and "goTab('bots')" in body


def test_topology_counters_without_a_list_are_not_fake_buttons():
    """У «Связей» и «Подписчиков» сводного списка нет — кнопки быть не должно."""
    body = _js_func("openTopology")
    m = re.search(r"Связей \(вступ\)", body)
    assert m, "счётчик связей пропал"
    card = body[max(0, m.start() - 220):m.start()]
    assert "tap-card" not in card, "«Связей» притворяется кнопкой, которая ведёт не туда"


def test_topology_empty_map_says_what_makes_a_link():
    body = _js_func("openTopology")
    assert "Связь появляется, когда" in body, "пустая карта ничего не объясняет"

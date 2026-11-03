"""Подсказки «Что делать дальше» не выталкивают список за первый экран.

Живой обход мини-аппа (03.11.2026): на вкладках Боты, Аккаунты и Рассылки
сверху стояли пять крупных карточек подсказок, и человек, открыв «Боты», не
видел ни одного бота без прокрутки. На «Итогах» подсказки — главное и остаются
целиком; на рабочих вкладках — одна главная и «Ещё N», раскрытие запоминается.
"""
from __future__ import annotations

from tests.miniapp_source import js_function


def test_work_tabs_show_one_tip_and_a_more_link():
    body = js_function("renderNextActions")
    assert "closest('#s-home')" in body, "вкладки не отличают «Итоги» от рабочих"
    assert "cards.slice(0, 1)" in body, "на рабочей вкладке снова все карточки сразу"
    assert "copilotToggle()" in body and "Ещё " in body


def test_the_choice_is_remembered():
    assert "infragram_copilot_expanded" in js_function("copilotExpanded")
    assert "infragram_copilot_expanded" in js_function("copilotToggle")

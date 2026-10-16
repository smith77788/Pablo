"""role="button" — обещание, которое надо исполнять с клавиатуры.

РАЗРЫВ. В мини-аппе 33 элемента (иконки в шапках, чипы срезов, действия в
строках) объявлены как `role="button"`. Озвучка читает такой элемент кнопкой и
предлагает нажать; спецификация WAI-ARIA требует, чтобы он отвечал на Enter и
на пробел. Отвечал он только на касание: ни одного обработчика клавиатуры в
приложении не было. Семнадцать из этих элементов вдобавок не имели `tabindex`,
то есть до них было не добраться даже табуляцией.

Получалась мёртвая кнопка — та же, что и всегда, только видит её не каждый: у
человека с клавиатурой или озвучкой действие просто не происходило, и ошибки
при этом не было.

ЗАМЕРЕНО ПОСЛЕ ПРАВКИ (Chromium): Enter и пробел на иконке строки вызывают её
действие, пробел при этом не прокручивает страницу, обычная <button> по-прежнему
срабатывает РОВНО один раз, фокус виден рамкой 2px.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html


def _handler() -> str:
    html = miniapp_html()
    i = html.index("document.addEventListener('keydown'")
    return html[i:html.index("\n});", i)]


def test_enter_and_space_activate_a_role_button():
    h = _handler()
    assert "ev.key !== 'Enter'" in h and "' '" in h, (
        "обработчик не реагирует на обе клавиши, которых требует WAI-ARIA")
    assert "getAttribute('role') !== 'button'" in h
    assert "el.click()" in h, "нажатие ничего не вызывает"


def test_native_controls_are_left_alone():
    """Иначе Enter на <button> вызовет действие дважды — форма уйдёт два раза."""
    h = _handler()
    for tag in ("'BUTTON'", "'A'", "'INPUT'", "'TEXTAREA'", "'SELECT'"):
        assert tag in h, f"{tag} не исключён — двойное срабатывание"


def test_space_does_not_scroll_the_page():
    h = _handler()
    assert "ev.preventDefault()" in h, (
        "пробел на кнопке прокрутит страницу вместе с нажатием")


def test_disabled_is_respected():
    h = _handler()
    assert "aria-disabled" in h, "нажатие сработает на отключённом элементе"


def test_every_role_button_can_be_focused():
    html = miniapp_html()
    tags = re.findall(r'<(?:div|span)\b[^>]*role="button"[^>]*>', html)
    assert tags, "разметка с role=\"button\" не найдена — проверка не о том"
    bad = [t for t in tags if "tabindex" not in t]
    assert not bad, (
        f"{len(bad)} элементов с role=\"button\" без tabindex — до них нельзя "
        "добраться табуляцией, и клавиатурная активация до них не дойдёт:\n  "
        + "\n  ".join(t[:90] for t in bad[:8]))


def test_focus_is_visible():
    html = miniapp_html()
    assert "focus-visible" in html, (
        "фокус не виден — человек не знает, на чём он стоит")

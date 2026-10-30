"""Полоса действий над выделением закрывала последние строки списка навсегда.

`.sel-bar` прибита к низу окна (`position:fixed`) и лежит ПОВЕРХ списка — места
в потоке она не занимает. Запас снизу у прокручиваемого ящика был рассчитан
ровно на нижнюю навигацию: `padding-bottom: var(--tab-h) + var(--safe-b) + 16px`.
Полоса выше неё и примерно вдвое толще, и всё, что под неё попадало, достать
было нечем: прокрутка уже в упоре.

Нашёл обход экранов (`deploy/scripts/render_miniapp.mjs`), когда научился
нажимать ту же ссылку «Все», что и владелец, и мерить закрытое в упоре
прокрутки: на вкладке «Аккаунты» закрыто 4 элемента, на вкладке «Боты» — 6.
Без режима выбора те же вкладки чистые — потому беду и не видели.

Полос выделения в приложении три (аккаунты, боты, каналы), и каждая
переключается в своём месте. Проверка требует, чтобы место списку выделяли все
три: пропущенная вернёт ровно прежнюю беду на одном экране, а отчёт обхода на
остальных будет зелёным.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html, miniapp_source

_WRAPS = ("accSelBar", "chSelBar", "botSelBar")


def _all_sources() -> str:
    return miniapp_source()


def test_the_scroll_box_reserves_room_for_the_bar():
    css = re.search(r"\.sb\{padding-bottom:[^}]*\}", miniapp_html())
    assert css, "у прокручиваемого ящика пропал запас снизу"
    assert "--sel-gap" in css.group(0), (
        "запас снизу снова рассчитан только на нижнюю навигацию — полоса "
        f"действий опять закроет хвост списка: {css.group(0)}")


def test_the_gap_is_measured_not_guessed():
    """Высота полосы зависит от числа кнопок и от ширины экрана."""
    src = miniapp_html()
    fn = re.search(r"function syncSelBarGap\([\s\S]{0,900}?\n\}", src)
    assert fn, "помощник, выделяющий полосе место, пропал"
    assert "getBoundingClientRect" in fn.group(0), (
        "высота полосы снова взята числом, а не померена: на 320 кнопки лягут "
        "в другое число рядов, и запас разойдётся с полосой")
    assert "setProperty('--sel-gap'" in fn.group(0)


def test_every_selection_bar_asks_for_its_room():
    """Пропущенная полоса вернёт прежнюю беду ровно на одном экране."""
    src = _all_sources()
    missing = [w for w in _WRAPS if f"syncSelBarGap('{w}')" not in src]
    assert not missing, (
        f"полосы выделения без выделенного места: {missing} — на этих экранах "
        "хвост списка снова не достать")


def test_the_room_is_taken_back_when_the_bar_closes():
    """Иначе внизу списка навсегда останется пустая полоса в сотню пикселей."""
    fn = re.search(r"function syncSelBarGap\([\s\S]{0,900}?\n\}", miniapp_html())
    assert fn and "'0px'" in fn.group(0), (
        "закрытая полоса больше не возвращает списку место")


def test_the_toggle_and_the_gap_are_not_separated():
    """Место выделяется там же, где полоса показывается, — иначе разойдутся."""
    src = _all_sources()
    for wrap in _WRAPS:
        m = re.search(rf"getElementById\('{wrap}'\)\.style\.display[^\n]*\n\s*"
                      rf"syncSelBarGap\('{wrap}'\)", src)
        assert m, (
            f"у {wrap} показ полосы и запас под неё разъехались по разным "
            "местам: следующая правка тронет одно и не тронет другое")

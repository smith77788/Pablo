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


def _block(src: str, head: str) -> str:
    """Тело функции по скобкам, а не по длине.

    Первая редакция брала его регуляркой с потолком в 1400 знаков: функция
    подросла на один комментарий, и проверки начали падать на здоровом коде —
    то есть мерить длину комментария вместо того, что в функции написано.
    """
    i = src.index(head)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"не закрыта скобка у {head!r}")


def _helper() -> str:
    src = miniapp_html()
    assert "function syncSelBarGap(" in src, (
        "помощник, выделяющий полосе место, пропал")
    return _block(src, "function syncSelBarGap(")


def _all_sources() -> str:
    return miniapp_source()


def test_the_room_goes_to_the_box_that_actually_scrolls():
    """Первая правка давала запас `.sb` — и на экране каналов не помогла.

    Там прокручивается не `.sb`, запас уходил туда, где его никто не крутит, и
    обход нашёл восемь закрытых элементов ровно на экране, про который уже было
    написано «исправлено». Ящик ищется по факту: `scrollHeight > clientHeight`.
    """
    fn = _helper()
    assert "scrollHeight - el.clientHeight" in fn, (
        "ящик для запаса снова выбирается по классу, а не по факту прокрутки")


def test_the_gap_is_measured_not_guessed():
    """Высота полосы зависит от числа кнопок и от ширины экрана."""
    fn = _helper()
    assert "getBoundingClientRect" in fn, (
        "высота полосы снова взята числом, а не померена: на 320 кнопки лягут "
        "в другое число рядов, и запас разойдётся с полосой")
    assert "paddingBottom" in fn and "getComputedStyle" in fn, (
        "запас больше не складывается с собственным отступом ящика")


def test_the_gap_does_not_pile_up_on_itself():
    """Базовый отступ читается, только когда свой запас уже снят."""
    fn = _helper()
    i_clear = fn.find("box.style.paddingBottom = ''")
    i_read = fn.find("getComputedStyle(box)")
    assert 0 <= i_clear < i_read, (
        "базовый отступ читается вместе со своим запасом — он сложится сам с "
        "собой на каждом переключении режима выбора")


def test_every_selection_bar_asks_for_its_room():
    """Пропущенная полоса вернёт прежнюю беду ровно на одном экране."""
    src = _all_sources()
    missing = [w for w in _WRAPS if f"syncSelBarGap('{w}')" not in src]
    assert not missing, (
        f"полосы выделения без выделенного места: {missing} — на этих экранах "
        "хвост списка снова не достать")


def test_the_room_is_taken_back_when_the_bar_closes():
    """Иначе внизу списка навсегда останется пустая полоса в сотню пикселей."""
    fn = _helper()
    assert re.search(r"box\.style\.paddingBottom = '';\s*\n\s*if \(!open\) return;", fn), (
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

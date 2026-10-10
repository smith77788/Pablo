"""Кнопка-значок без подписи для скринридера — просто «кнопка».

В разметке было 277 кликабельных элементов, у которых внутри только значок: ←,
✕, ☰, 🗑, ➤. Ни aria-label, ни title — незрячему пользователю приложение
называло их одним словом «кнопка», и отличить «Назад» от «Удалить» было нельзя.
Часть из них ещё и `div`/`span` без role и tabindex: для клавиатуры и
скринридера такой элемент вообще не кнопка, а текст.

Подпись берётся ПО ОБРАБОТЧИКУ, а не по значку. Первая редакция правки
подписывала по значку и выдала две прямые неправды: крестики у строк списка
(`deleteDl`, `deleteCmd`) удаляют запись, а получили «Закрыть». Значок говорит,
как элемент выглядит; имя обработчика — что произойдёт.

Правило: у кликабельного элемента, внутри которого нет ни одной буквы и ни одной
цифры, должно быть доступное имя.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_html

_CLICKABLE = re.compile(r'<(button|span|div)([^>]*\bonclick=[^>]*?)>([^<]{0,12})</\1>')
_HAS_LETTER = re.compile(r"[A-Za-zА-Яа-я0-9]")


def _icon_only():
    for tag, attrs, txt in _CLICKABLE.findall(miniapp_html()):
        t = txt.strip()
        if not t or _HAS_LETTER.search(t):
            continue
        yield tag, attrs, t


def test_detector_still_sees_the_icon_buttons():
    """Если их вдруг «не стало» — сломан разбор, а не разметка."""
    assert len(list(_icon_only())) > 200


def test_every_icon_button_has_an_accessible_name():
    nameless = [
        (t, re.sub(r"\s+", " ", attrs)[:70])
        for _, attrs, t in _icon_only()
        if "aria-label" not in attrs and "title=" not in attrs
    ]
    assert not nameless, (
        f"{len(nameless)} кнопок-значков без подписи — скринридер назовёт их просто "
        f"«кнопка»: {nameless[:5]}"
    )


def test_div_and_span_buttons_are_reachable():
    """`div` с onclick — кнопка только для мыши, пока нет role и tabindex."""
    bad = [
        (t, re.sub(r"\s+", " ", attrs)[:70])
        for tag, attrs, t in _icon_only()
        if tag in ("div", "span") and ("role=" not in attrs or "tabindex=" not in attrs)
    ]
    assert not bad, f"{len(bad)} значков-кнопок недоступны с клавиатуры: {bad[:5]}"


def test_destructive_controls_are_not_called_closing():
    """«Закрыть» на кнопке, которая удаляет запись, — прямая неправда."""
    wrong = []
    for _, attrs, _t in _icon_only():
        oc = re.search(r'onclick="\s*(?:event\.stopPropagation\(\);)?\s*([A-Za-z_$][\w$]*)\s*\(', attrs)
        al = re.search(r'aria-label="([^"]*)"', attrs)
        if not oc or not al:
            continue
        if re.match(r"^(delete|remove|del)[A-Z_]", oc.group(1)) and "Закрыть" in al.group(1):
            wrong.append((oc.group(1), al.group(1)))
    assert not wrong, f"удаление подписано как закрытие: {wrong}"


# Обозначения форматов и протоколов по-русски не пишут: «Экспорт CSV» владельцу
# понятнее, чем «Экспорт таблицы с разделителями». Английские СЛОВА — нет.
_ACRONYMS = {"CSV", "JSON", "API", "SMS", "IP", "URL", "ID", "QR", "SEO", "DM",
             "HTML", "PDF", "XLSX", "TXT", "SOCKS", "HTTP", "HTTPS", "TON"}


def test_names_are_russian():
    """Владелец не читает по-английски (CLAUDE.md)."""
    latin = []
    for _, attrs, _t in _icon_only():
        al = re.search(r'aria-label="([^"]*)"', attrs)
        if not al:
            continue
        words = [w for w in re.findall(r"[A-Za-z]{2,}", al.group(1))
                 if w.upper() not in _ACRONYMS]
        if words:
            latin.append(al.group(1))
    assert not latin, f"английские подписи: {sorted(set(latin))[:5]}"

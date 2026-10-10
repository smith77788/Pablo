"""Из ошибки в мини-аппе всегда есть выход.

Экран, открытый с вкладки, лежит в стеке ОДИН. Таббар на подэкране скрыт
(`showTabbar(false)`), поэтому если блок ошибки не дал ни одной кнопки —
пользователю остаётся только закрыть мини-апп. Ровно это и происходило:

* `errHtml()` и `empty()` показывали «Назад» при `STACK.length > 1`, то есть
  НЕ показывали при глубине 1 — в самом частом случае;
* четыре экрана (облако, командный центр, менеджер продаж, предохранитель
  флуда) рисовали ошибку своей вёрсткой мимо `errHtml` — красный текст и
  больше ничего.

Замер: пробник открывал 161 экран при ответе сервера 500 и смотрел, есть ли
в теле экрана хоть один интерактивный элемент.
"""
from __future__ import annotations

import re

from tests.miniapp_source import miniapp_source, screen_files


def _body(src: str, header: str) -> str:
    """Тело функции от её заголовка до начала следующей функции верхнего уровня."""
    i = src.index(header)
    m = re.compile(r"\n(?:async )?function ").search(src, i + len(header))
    return src[i : m.start() if m else len(src)]


def test_zapasnoy_vyhod_ne_zavisit_ot_glubiny_stacka():
    src = miniapp_source()
    for header in ("function errHtml(", "function empty("):
        body = _body(src, header)
        assert "STACK.length > 1" not in body, (
            f"{header}…) показывает «Назад» только при STACK.length > 1. "
            "Экран, открытый с вкладки, лежит в стеке один — кнопки не будет "
            "вовсе, а таббар на подэкране скрыт. Условие должно быть "
            "`STACK.length`: back() при глубине 1 возвращает на экран вкладки."
        )


def test_oshibka_ekrana_ne_risuetsya_mimo_errHtml():
    # Голый красный текст во всё тело экрана: ни кнопки повтора, ни «Назад».
    golyi = re.compile(
        r"body\.innerHTML\s*=\s*'<div style=\"color:var\(--red\);padding:20px\">'"
    )
    for path in screen_files():
        text = path.read_text(encoding="utf-8")
        assert not golyi.search(text), (
            f"{path.name}: тело экрана при ошибке заполняется своей вёрсткой "
            "без единой кнопки. Используйте errHtml(сообщение, 'повторитьВызов()') "
            "— он всегда оставляет выход."
        )

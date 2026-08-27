"""Плашка исхода операции — точка действия, а не тупик.

Раньше при провале операции плашка показывала причину и вела только «в детали»:
чтобы повторить, пользователь шёл в раздел «Операции» и искал её — лишние
переходы. Теперь при ошибке в самой плашке есть «↻ Повторить» (один тап,
переиспользует retryOp → /operation/{id}/retry). Успешная плашка кнопки не имеет.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "mini_app", "index.html")


def _fn_source(name: str) -> str:
    html = open(INDEX, encoding="utf-8").read()
    i = html.find(f"function {name}(")
    assert i != -1, f"не найдена функция {name}"
    # грубая вырезка тела функции: до следующего объявления функции верхнего уровня
    j = html.find("\nfunction ", i + 1)
    return html[i: j if j != -1 else i + 2000]


def test_failure_banner_has_one_tap_retry():
    src = _fn_source("showOpComplete")
    # повтор только при ошибке (ok ? '' : ...)
    assert "retryOp(" in src, "в плашке исхода нет повтора операции"
    assert "Повторить" in src
    # повтор под условием НЕ-успеха (тернар ok ? '' : кнопка)
    assert re.search(r"ok\s*\?\s*''\s*:", src), \
        "кнопка повтора должна показываться только при ошибке (ok ? '' : ...)"


def test_retry_uses_existing_endpoint():
    html = open(INDEX, encoding="utf-8").read()
    assert "/api/miniapp/operation/'+id+'/retry" in html or \
           "/api/miniapp/operation/${id}/retry" in html, \
        "retryOp не бьёт в существующий эндпоинт повтора операции"

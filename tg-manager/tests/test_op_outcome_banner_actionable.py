"""Плашка исхода операции — точка действия, а не тупик.

Раньше при провале операции плашка показывала причину и вела только «в детали»:
чтобы повторить, пользователь шёл в раздел «Операции» и искал её — лишние
переходы. Теперь при ошибке в самой плашке есть «↻ Повторить» (один тап,
переиспользует retryOp → /operation/{id}/retry). При успехе для операций с
логичным продолжением (инвайт→приветствие, регистрация→прогрев) плашка даёт
кнопку следующего шага — те же цепочки, что валидированы Copilot'ом.
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
    # повтор только при ошибке (ok ? <next> : <retry>)
    assert "retryOp(" in src, "в плашке исхода нет повтора операции"
    assert "Повторить" in src
    # повтор — в false-ветке тернара ok (при успехе — следующий шаг, не повтор)
    assert re.search(r"ok\s*\?\s*_opNextBtn\(op\)\s*:", src), \
        "успех → _opNextBtn(op), ошибка → кнопка повтора"


def test_success_banner_offers_next_step():
    html = open(INDEX, encoding="utf-8").read()
    # карта следующих шагов и её применение в плашке
    assert "const _OP_NEXT" in html and "function _opNextBtn" in html
    assert "_opNextBtn(op)" in _fn_source("showOpComplete")


def test_next_step_targets_are_real_and_guarded():
    html = open(INDEX, encoding="utf-8").read()
    i = html.find("const _OP_NEXT")
    body = html[i:i + 500]
    fns = set(re.findall(r"fn:'([a-zA-Z0-9_]+)'", body))
    assert fns, "в _OP_NEXT нет целей"
    for fn in fns:
        assert re.search(rf"function {fn}\b", html), f"цель {fn} не существует — мёртвый тап"
    # кнопка рендерится только если функция есть (защита от мёртвого тапа)
    assert "typeof window[n.fn] !== 'function'" in html


def test_next_step_chains_match_copilot():
    """Ключи _OP_NEXT совпадают с триггерами тех же цепочек в next_actions —
    баннер не выдумывает переходы, а ускоряет уже валидированные."""
    html = open(INDEX, encoding="utf-8").read()
    i = html.find("const _OP_NEXT")
    body = html[i:i + 500]
    keys = set(re.findall(r"^\s*([a-z_]+):\s*\{", body, re.M))
    na = open(os.path.join(ROOT, "services", "next_actions.py"), encoding="utf-8").read()
    # engage_after_invite ← mass_invite; warmup_after_register ← auto_register/reg_check/bot_factory
    assert "mass_invite" in keys
    assert {"auto_register", "reg_check", "bot_factory"} & keys
    for k in keys:
        assert f'"{k}"' in na, f"op_type {k} не встречается как триггер в next_actions"


def test_retry_uses_existing_endpoint():
    html = open(INDEX, encoding="utf-8").read()
    assert "/api/miniapp/operation/'+id+'/retry" in html or \
           "/api/miniapp/operation/${id}/retry" in html, \
        "retryOp не бьёт в существующий эндпоинт повтора операции"

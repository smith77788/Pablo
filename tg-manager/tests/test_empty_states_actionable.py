"""Пустые экраны на пути «контент/сообщения» ведут к следующему шагу (не тупик).

Жалоба «нету информации / MVP»: пустые экраны показывали «Нет …» без действия.
empty() поддерживает CTA-кнопку {label, fn}. Довели ключевые on-journey пустые
состояния до «объяснение + кнопка создать», а не тупик.
"""
from __future__ import annotations

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def _empty_call_with(marker):
    # находит вызов empty(...), содержащий marker, в одной строке
    for line in HTML.splitlines():
        if "empty(" in line and marker in line:
            return line
    return ""


def test_autoreplies_empty_has_cta():
    line = _empty_call_with("Нет авто-ответов")
    assert "openArModal()" in line, line


def test_dm_campaigns_empty_has_cta():
    line = _empty_call_with("Нет DM-кампаний")
    assert "openCmpModal()" in line and "Создать кампанию" in line, line


def test_templates_empty_has_cta():
    line = _empty_call_with("Нет шаблонов")
    assert "openTplModal()" in line, line


def test_cta_targets_are_real_functions():
    for fn in ("openArModal", "openCmpModal", "openTplModal"):
        assert re.search(rf"function {fn}\(", HTML), f"{fn} не определена"


def test_empty_error_states_offer_retry_at_root():
    # На корневой вкладке ошибка загрузки должна давать «Повторить» (reload),
    # а не только «Назад» — 73 error-состояния раньше упирались в текст ошибки.
    assert re.search(r"function reloadView\(", HTML), "reloadView не определена"
    i = HTML.find("function empty(")
    seg = HTML[i:i + 1400]
    # ветка ошибки на корне зовёт reloadView
    assert "reloadView()" in seg and "↻ Повторить" in seg, seg[:400]
    # условие срабатывает только для error-иконки на корневом экране
    assert "isErr && atRoot" in seg


def test_reloadview_is_safe_on_substacks():
    # reloadView не перезагружает вкладку, когда открыт вложенный экран
    # (иначе обновился бы не тот экран). Гейт против регрессии.
    i = HTML.find("function reloadView(")
    seg = HTML[i:i + 300]
    assert "STACK.length) return" in seg

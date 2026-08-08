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

"""Понятность: у секций каталога и у модулей есть описания «что это / что можно».

Section-level (под .sec) + пер-модульные (MODULE_DESCS → подпись в боковом меню +
tooltip на плитках). Так пользователь понимает, куда попал.
"""
from __future__ import annotations
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ui():
    with open(os.path.join(ROOT, "mini_app/index.html"), encoding="utf-8") as f:
        return f.read()


def test_section_descriptions_present():
    ui = _ui()
    # все ключевые секции каталога аннотированы
    for name in ("Основное", "Управление аккаунтами", "Продвижение",
                 "Безопасность", "Фабрики"):
        assert f'class="sec">{name}</div>' in ui
    assert ui.count('margin:-6px 0 8px') >= 9  # 9 section-desc


def test_module_descriptions_wired():
    ui = _ui()
    assert "const MODULE_DESCS" in ui
    # подпись реально рендерится в drawer + tooltip на плитке
    assert "MODULE_DESCS[lbl]" in ui and "descHtml" in ui
    assert "tile.title = desc" in ui
    # покрытие: большинство названий плиток каталога имеют описание
    labels = set(re.findall(r'mgmt-tile-lbl">([^<]+)<', ui))
    m = re.search(r'const MODULE_DESCS = \{(.*?)\n\};', ui, re.S)
    described = set(re.findall(r"'([^']+)'\s*:", m.group(1)))
    covered = [l for l in labels if l.strip() in described]
    # хотя бы 70% плиток описаны (часть — дубли/служебные)
    assert len(covered) >= int(len(labels) * 0.7), (len(covered), len(labels))

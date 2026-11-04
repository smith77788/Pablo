"""«Авто-воронки» и «Воркфлоу» — один модуль «Сценарии», дубль не возвращается.

Авто-воронка давно исполняется движком сценариев (schema_v283,
services/legacy_funnels.py), а в каталоге стояли две плитки про одно и то
же. Теперь одна плитка и две вкладки; старые входы (openAutoFunnels из
карточки бота и подсказок «что дальше», openWorkflows) открывают свою
вкладку модуля.
"""
from __future__ import annotations

import re

from services.task_sections import section_of_tile
from tests.miniapp_source import miniapp_html, miniapp_source

HTML = miniapp_html()
SRC = miniapp_source()


def _catalog_labels() -> list[str]:
    s = HTML.index('<div class="screen" id="s-more">')
    cat = HTML[s:HTML.index("<!-- Админ-панель (только для админов) -->", s)]
    return re.findall(r'mgmt-tile-lbl">([^<]*)<', cat)


def _fn_body(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\([^)]*\)\s*\{", SRC)
    assert m, f"функции {name} нет"
    depth, i = 0, m.end() - 1
    while True:
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        i += 1
        if depth == 0:
            return SRC[m.start():i]


def test_one_tile_in_the_catalog():
    labels = _catalog_labels()
    assert labels.count("Сценарии") == 1
    for old in ("Авто-воронки", "Воркфлоу"):
        assert old not in labels, f"плитка «{old}» вернулась рядом со «Сценариями»"
        assert section_of_tile(old) is None, old
    assert section_of_tile("Сценарии").key == "messages"
    assert 'onclick="openScenarios()"' in HTML


def test_both_screens_are_tabs_of_one_module():
    reg = re.search(r"const SCREEN_MODULES = \{(.*?)\n\};", SRC, re.S).group(1)
    for key, sid in (("all", "s-workflows"), ("funnels", "s-autofunnels")):
        assert re.search(r"key:'" + key + r"'[^\n]*sid:'" + sid + "'", reg), key
        head = HTML[HTML.index(f'<div class="screen" id="{sid}">'):][:900]
        assert '<div class="hdr-title">🔄 Сценарии</div>' in head, sid
        assert f'data-module="scenarios" data-tab="{key}"' in head, sid


def test_old_entries_still_open_their_tab():
    """Карточка бота и «что дальше» зовут openAutoFunnels — он жив и
    подсвечивает вкладку модуля, а не открывает отдельный раздел."""
    assert "_markModTab('scenarios', 'funnels')" in _fn_body("openAutoFunnels")
    assert "_markModTab('scenarios', 'all')" in _fn_body("openWorkflows")
    assert "modTab('scenarios'" in _fn_body("openScenarios")

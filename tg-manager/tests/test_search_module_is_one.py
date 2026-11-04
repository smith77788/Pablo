"""«Поиск Telegram» — один модуль, а не шесть плиток.

ЧТО БЫЛО. В каталоге «Разделы» жили «Позиции», «Позиции в поиске», «Рейтинг»
(Итоги), «Глобальный поиск» (Аудитория), «SEO» и «Топ поиска» (Каналы и боты).
«Позиции» и «Рейтинг» читали одни и те же `tracked_keywords` + `search_rankings`
двумя разными ручками, а экраны «Позиции» и «Позиции в поиске» назывались
одинаково, хотя показывали разное (боты и каналы). Владелец: «в принципе это
всё должен был быть один полноценный модуль».

ЧТО СТЕРЕЖЁМ.
  * в каталоге одна плитка модуля и ни одной из шести старых;
  * каждая вкладка модуля — существующий экран со своей полоской вкладок и
    общей шапкой, а его open-функция жива (на неё ведут карточка бота,
    подсказки «что дальше», аналитика) и открывает именно его;
  * переключение вкладок заменяет вкладку в стеке: иначе «назад» перебирал бы
    все открытые вкладки по одной, прежде чем выйти из модуля;
  * дубль экрана «Позиции» и его ручки (/keywords, /keyword) не вернулись.
"""
from __future__ import annotations

import re

from services.task_sections import section_of_tile
from tests.miniapp_routes import registered_routes
from tests.miniapp_source import miniapp_html, miniapp_source

HTML = miniapp_html()
SRC = miniapp_source()

OLD_TILES = ("Позиции", "Позиции в поиске", "Рейтинг", "Глобальный поиск",
             "SEO", "Топ поиска")
TABS = {  # ключ вкладки → (экран, функция, которая его открывает)
    "bots": ("s-ranking", "openRanking"),
    "channels": ("s-chranks", "openChannelRanks"),
    "serp": ("s-gsearch", "openGlobalSearch"),
    "seo": ("s-seo", "openSeo"),
    "top": ("s-promo", "openPromo"),
}


def _catalog() -> str:
    s = HTML.index('<div class="screen" id="s-more">')
    return HTML[s:HTML.index("<!-- Админ-панель (только для админов) -->", s)]


def _fn_body(name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + name + r"\s*\([^)]*\)\s*\{", SRC)
    assert m, f"функции {name} нет"
    depth, i = 0, m.end() - 1
    while True:
        depth += {"{": 1, "}": -1}.get(SRC[i], 0)
        i += 1
        if depth == 0:
            return SRC[m.start():i]


def _registry() -> str:
    m = re.search(r"const SCREEN_MODULES = \{(.*?)\n\};", SRC, re.S)
    assert m, "реестра модулей SCREEN_MODULES нет"
    return m.group(1)


def test_one_tile_instead_of_six():
    labels = re.findall(r'mgmt-tile-lbl">([^<]*)<', _catalog())
    assert labels.count("Поиск Telegram") == 1
    back = [t for t in OLD_TILES if t in labels]
    assert not back, f"старые плитки вернулись в каталог: {back}"
    assert 'onclick="openSearchHub()"' in _catalog()
    assert section_of_tile("Поиск Telegram") is not None
    for t in OLD_TILES:
        assert section_of_tile(t) is None, f"«{t}» снова в карте разделов"


def test_every_tab_is_a_live_screen_with_the_tab_strip():
    reg = _registry()
    for key, (sid, fn) in TABS.items():
        assert re.search(r"key:'" + key + r"'.*?sid:'" + sid + r"'.*?" + fn + r"\(\)", reg), (
            f"вкладка {key} не ведёт на {sid} через {fn}()")
        s = HTML.index(f'<div class="screen" id="{sid}">')
        head = HTML[s:s + 1200]
        assert '<div class="hdr-title">🔎 Поиск Telegram</div>' in head, (
            f"{sid}: шапка не называет модуль — непонятно, где находишься")
        assert f'data-module="search" data-tab="{key}"' in head, (
            f"{sid}: нет полоски вкладок модуля")
        assert f"push('{sid}')" in _fn_body(fn), f"{fn}() больше не открывает {sid}"


def test_switching_tabs_replaces_instead_of_stacking():
    body = _fn_body("modTab")
    assert "m.tabs.some(x => x.sid === top)" in body
    assert "STACK.pop()" in body, "вкладка ложится поверх — «назад» переберёт их все"
    assert "t.open()" in body


def test_duplicate_positions_screen_and_routes_are_gone():
    assert 'id="s-keywords"' not in HTML
    for fn in ("openKeywordsScreen", "loadKeywords", "saveKw", "deleteKw"):
        assert not re.search(r"function\s+" + fn + r"\b", SRC), f"дубль {fn} вернулся"
    live = set(registered_routes())
    for r in ("GET /api/miniapp/keywords", "POST /api/miniapp/keyword",
              "DELETE /api/miniapp/keyword/{kw_id}"):
        assert r not in live, f"дубль-ручка {r} вернулась рядом с /ranking/*"

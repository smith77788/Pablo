"""Одна карта разделов на бот и мини-апп.

Интерфейс был построен вокруг устройства системы: в боте восемь разделов
(«Активы & Сети», «Мониторинг & Защита»…) по 8–16 кнопок, в мини-аппе
одиннадцать других («Основное», «Расширенные», «Дополнительно»), и одну
функцию искали в двух местах. Теперь разделы по задачам человека заданы ОДИН
раз — `services/task_sections.py` — и по этой карте собраны и экран
«Разделы» мини-аппа (а с ним боковое меню), и главное меню бота.

Проверяется, что карта и разметка не разъехались:
  * каждая плитка каталога лежит ровно в одном разделе, и именно в том, что
    велит карта, — разметку правят руками, и плитка, добавленная «куда-нибудь»,
    тихо возвращает старую свалку;
  * разделы идут в порядке карты;
  * Лаборатория скрыта по умолчанию, но её плитки остаются в разметке
    (у каждого экрана есть дверь) и находятся поиском;
  * «Массовые жалобы» и «Накрутка» — в обычном разделе, а не в Лаборатории:
    владелец решил оставить их на виду (01.11.2026);
  * бот показывает те же шесть разделов и для каждого есть обработчик.
"""
from __future__ import annotations

import ast
import pathlib
import re

from services.task_sections import BY_KEY, CORE, SECTIONS, section_of_tile
from tests.miniapp_source import miniapp_html, miniapp_source

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = miniapp_html()
BOTMENU = (ROOT / "bot" / "handlers" / "botmother_menu.py").read_text(encoding="utf-8")


def _catalog() -> str:
    s = HTML.index('<div class="screen" id="s-more">')
    e = HTML.index("<!-- Админ-панель (только для админов) -->", s)
    return HTML[s:e]


def _grids() -> dict[str, list[str]]:
    """Ключ раздела → подписи плиток в его сетке, в порядке разметки."""
    cat = _catalog()
    out: dict[str, list[str]] = {}
    for m in re.finditer(r'<div class="mgmt-grid" data-section="([a-z_]+)">', cat):
        nxt = cat.find('<div class="mgmt-grid"', m.end())
        body = cat[m.end(): nxt if nxt != -1 else len(cat)]
        out[m.group(1)] = [x.strip() for x in re.findall(r'mgmt-tile-lbl">([^<]*)<', body)]
    return out


def test_every_tile_sits_in_its_section():
    grids = _grids()
    assert set(grids) == set(BY_KEY), (sorted(grids), sorted(BY_KEY))
    for key, labels in grids.items():
        for lbl in labels:
            sec = section_of_tile(lbl)
            assert sec is not None, f"плитки «{lbl}» нет в карте services/task_sections.py"
            assert sec.key == key, f"«{lbl}» лежит в «{key}», а карта велит «{sec.key}»"
        assert labels == list(BY_KEY[key].tiles), (
            f"состав раздела «{key}» в разметке и в карте разошёлся")


def test_no_tile_in_two_sections():
    seen: dict[str, str] = {}
    for sec in SECTIONS:
        for lbl in sec.tiles:
            assert lbl not in seen, f"«{lbl}» и в «{seen[lbl]}», и в «{sec.key}»"
            seen[lbl] = sec.key


def test_sections_in_map_order():
    order = re.findall(r'<div class="sec" data-section="([a-z_]+)"', _catalog())
    assert order == [s.key for s in SECTIONS]


def test_six_task_sections():
    assert [s.title for s in CORE] == [
        "Итоги", "Аудитория", "Сообщения", "Каналы и боты", "Продажи", "Аккаунты"]


def test_lab_hidden_by_default_but_reachable():
    lab = BY_KEY["lab"]
    assert lab.hidden_by_default
    assert '#s-more.lab-off:not(.lab-search) [data-section="lab"]' in HTML
    src = miniapp_source()
    # По умолчанию выключена: без явного «1» в хранилище.
    assert "localStorage.getItem('ig_lab_on') === '1'" in src
    # Поиск снимает скрытие на время запроса.
    assert "more.classList.toggle('lab-search', !!query)" in src
    # Переключатель есть в разметке.
    assert 'onclick="toggleLab()"' in HTML
    for lbl in lab.tiles:
        assert f'mgmt-tile-lbl">{lbl}<' in HTML, lbl


def test_owner_kept_risky_tools_visible():
    for lbl in ("Массовые жалобы", "Накрутка"):
        sec = section_of_tile(lbl)
        assert sec is not None and not sec.hidden_by_default, lbl


def test_section_deeplink_opens_section():
    src = miniapp_source()
    assert "if (k==='section' && prm) return openSection(String(prm));" in src
    assert re.search(r"function openSection\(key\)", src)


def test_bot_main_menu_uses_the_map():
    tree = ast.parse(BOTMENU)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_main_menu_kb")
    body = ast.unparse(fn)
    assert "from services.task_sections import CORE" in body
    assert 'BmCb(action="section", sub=sec.key)' in body.replace("'", '"')
    assert 'BmCb(action="summary")' in body.replace("'", '"')
    assert 'BmCb(action="tools")' in body.replace("'", '"')


def test_bot_has_buttons_for_every_task_section():
    from bot.handlers.botmother_menu import _section_buttons

    for sec in CORE:
        if sec.key == "overview":
            continue   # «Итоги» в боте — отдельная кнопка со сводкой
        assert _section_buttons(sec.key), f"у раздела «{sec.title}» в боте нет действий"


def test_bot_handlers_for_new_actions():
    for action in ("summary", "section", "tools"):
        assert f'BmCb.filter(F.action == "{action}")' in BOTMENU, action

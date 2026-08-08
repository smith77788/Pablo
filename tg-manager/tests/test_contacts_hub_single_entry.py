"""Хаб контактов — ОДИН вход, а не восемь плиток на один модуль.

ЧТО БЫЛО. «Управление» показывало 8 отдельных плиток, каждая — часть одного и
того же модуля контактов: Контакты, Синхронизация, Граф связей, Дубликаты,
Конфликты, Умные теги, Напоминания, Экспорт. Хуже того, синхронизация жила в
ДВУХ местах: иконка 🔄 в шапке контактов запускала синхронизацию сразу, а
отдельная плитка «Синхронизация» открывала целый «центр синхронизации». Снаружи
это и читалось как «контакты и синхронизация — почему-то два разных модуля».

КАК ДОЛЖНО. Ровно ОДНА плитка «Контакты» ведёт в хаб; внутри хаба — панель
инструментов, откуда достижимы синхронизация/граф/дубликаты/конфликты/теги/
напоминания/экспорт. Синхронизация — одна поверхность (центр синхронизации,
где виден статус и есть кнопка запуска).

Гейт держит инвариант «один вход»: инструменты хаба не должны снова расползтись
в плитки «Управления», и каждый обязан остаться достижимым из самого хаба.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")

_DIV = re.compile(r"<(/?)div\b", re.I)

# Инструменты, которые обязаны жить ВНУТРИ хаба, а не отдельными плитками.
HUB_TOOLS = [
    "openUchSyncCenter",
    "openUchGraph",
    "detectDuplicates",
    "openUchConflicts",
    "openUchSmartTags",
    "openUchReminders",
    "openUchExport",
]


def _screen_body(sid: str) -> str:
    m = re.search(rf'<div class="screen" id="{re.escape(sid)}"', HTML)
    assert m, f"экран {sid} не найден"
    start, depth = m.start(), 0
    for t in _DIV.finditer(HTML, start):
        depth += -1 if t.group(1) else 1
        if depth == 0:
            return HTML[start:t.end()]
    raise AssertionError(f"незакрытый экран {sid}")


def _mgmt_tile_handlers() -> set[str]:
    return set(re.findall(r'<div class="mgmt-tile" onclick="([A-Za-z0-9_]+)\(', HTML))


def test_tools_are_not_management_tiles():
    """Инструменты хаба не должны снова стать плитками «Управления»."""
    tiles = _mgmt_tile_handlers()
    leaked = [fn for fn in HUB_TOOLS if fn in tiles]
    assert not leaked, (
        f"инструменты хаба снова вынесены в плитки: {leaked}. "
        "Их место — панель инструментов внутри экрана «Контакты», один вход."
    )


def test_tools_reachable_from_hub():
    """Каждый инструмент достижим из самого хаба контактов."""
    body = _screen_body("s-contacts")
    missing = [fn for fn in HUB_TOOLS if f"{fn}(" not in body]
    assert not missing, (
        f"инструменты недостижимы из хаба: {missing} — убрали плитку, но не дали "
        "входа внутри «Контактов»: экран стал бы сиротой."
    )


def test_single_contacts_tile():
    """Ровно одна плитка ведёт в модуль контактов — сам хаб."""
    tiles = _mgmt_tile_handlers()
    assert "openContacts" in tiles, "плитка «Контакты» пропала — в хаб нет входа"


def test_sync_is_single_surface():
    """Синхронизация — одна поверхность: центр синхронизации.

    Слепой запуск syncContacts() из шапки контактов (в обход центра, где виден
    статус) был вторым, конкурирующим входом — именно он путал.
    """
    header = _screen_body("s-contacts")
    # В хабе к синхронизации ведёт центр синхронизации, а не немедленный запуск.
    assert "openUchSyncCenter(" in header, "из хаба нет входа в центр синхронизации"
    assert "syncContacts(" not in header, (
        "синхронизация снова запускается прямо из контактов в обход центра — "
        "это второй вход и та самая путаница «контакты vs синхронизация»"
    )
    # Запуск синхронизации остаётся — но ровно в центре синхронизации.
    center = _screen_body("s-uchsynccenter")
    assert "s-uchsynccenter" in center


def test_sync_center_still_runs_sync():
    """Кнопка запуска не потеряна: она в центре синхронизации."""
    # syncContacts как функция остаётся — её дёргает кнопка центра синхронизации.
    assert "function syncContacts(" in HTML, "функция синхронизации исчезла"
    assert 'onclick="syncContacts()"' in HTML, (
        "кнопка «Синхронизировать все» в центре синхронизации потеряна"
    )

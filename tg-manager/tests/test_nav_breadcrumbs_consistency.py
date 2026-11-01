"""Навигационные breadcrumbs в текстах бота должны соответствовать реальному меню.

Прод-жалоба: «описания по всему боту не соответствуют реальности». Приветствие
/start вело в «📱 Аккаунты & Боты → …», а такого пункта в меню нет — реальный
раздел называется «🏗 Активы & Сети» (botmother_menu._assets_kb). Часть подсказок
вообще отправляла добавлять аккаунты в «Мониторинг», хотя аккаунты/боты/каналы —
в «Активы & Сети».

Тест запрещает устаревшие ярлыки навигации во всех текстах бота и требует, чтобы
каноничный ярлык раздела совпадал с кнопкой в меню.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCAN_DIRS = ["bot", "services"]

# Устаревшие/неверные формы навигации → не должны встречаться в исходниках.
_FORBIDDEN = [
    "Аккаунты & Боты",          # раздел переименован в «Активы & Сети»
    "Аккаунты и Боты",
    "📱 Активы →",               # неверный ярлык: реальный — «🏗 Активы & Сети»
    "📱 Активы <",
    "Мониторинг → 📱 Аккаунт",   # аккаунты добавляются в «Активы & Сети», не в «Мониторинг»
]


def _iter_py():
    for d in _SCAN_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if "__pycache__" in str(p):
                continue
            yield p


def test_no_stale_navigation_labels():
    hits = []
    for p in _iter_py():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in _FORBIDDEN:
            if bad in text:
                hits.append(f"{p.relative_to(ROOT)}: '{bad}'")
    assert not hits, "Устаревшие навигационные ярлыки:\n" + "\n".join(hits)


def test_canonical_assets_label_matches_menu_button():
    """Приветствие /start ведёт в разделы, которые реально есть в главном меню.

    01.11.2026 главное меню перестроено по разделам-задачам из одной карты
    services/task_sections.py: «/menu → 📡 Каналы и боты» и «/menu → 📱
    Аккаунты» — это заголовки разделов карты, а кнопки меню строятся из неё.
    Прежний раздел «🏗 Активы & Сети» жив за «🧰 Все инструменты бота»."""
    from services.task_sections import BY_KEY

    menu = (ROOT / "bot" / "handlers" / "botmother_menu.py").read_text(encoding="utf-8")
    assert 'text="🏗 Активы & Сети"' in menu
    start = (ROOT / "bot" / "handlers" / "start.py").read_text(encoding="utf-8")
    for key in ("channels", "accounts"):
        sec = BY_KEY[key]
        assert f"/menu → {sec.emoji} {sec.title}" in start, sec.title

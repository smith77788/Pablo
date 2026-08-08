"""Взаимные контакты: считаются, показываются в KPI и объяснены в UI.

Пользователь спросил, что такое «взаимные» и почему их 2. Это корректно (флаг
Telegram: обе стороны добавили друг друга), но было неочевидно. Добавили счётчик
в KPI и пояснение при выборе фильтра.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = (ROOT / "services" / "contacts_hub" / "repository.py").read_text(encoding="utf-8")
HTML = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_stats_expose_mutual_and_phone():
    assert "'mutual': mutual" in REPO, "stats не отдают счётчик взаимных"
    assert "is_mutual=TRUE" in REPO, "нет запроса взаимных"
    assert "'with_phone': with_phone" in REPO, "нет счётчика с телефоном"


def test_kpi_shows_mutual_and_phone():
    assert "↔ Взаимные" in HTML and "d.mutual" in HTML, "KPI не показывает взаимных"
    assert "📱 Телефон" in HTML and "d.with_phone" in HTML, "KPI не показывает с телефоном"


def test_mutual_filter_has_explanation():
    assert "CONTACT_FILTER_HINTS" in HTML, "нет пояснений к фильтрам"
    assert "двусторонняя связь" in HTML.lower() or "двусторонн" in HTML.lower(), \
        "нет объяснения взаимности"
    assert 'id="contactFilterHint"' in HTML, "нет элемента для подсказки фильтра"

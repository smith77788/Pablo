"""Номер страницы между стрелками листания.

Списки в боте листались стрелками «◀️ ▶️» без номера: при десятке страниц
не видно, где ты и сколько осталось. Кнопка с номером ведёт на ту же
страницу (перерисовка), то есть ничего не ломает, если её нажать.
"""
from __future__ import annotations


def page_label(page: int, total: int, per_page: int) -> str | None:
    """«📄 2/7» или None, если страница одна (тогда номер не нужен)."""
    if per_page <= 0 or total <= per_page:
        return None
    pages = (total + per_page - 1) // per_page
    return f"📄 {min(page, pages - 1) + 1}/{pages}"

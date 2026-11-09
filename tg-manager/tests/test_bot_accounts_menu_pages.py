"""«Мои аккаунты» в боте листается страницами, а не строит кнопку на каждый аккаунт.

НАХОДКА (09.11.2026, прогон боевого диспетчера на базе с 10 000 аккаунтов).
Меню строило кнопку и строку текста на КАЖДЫЙ аккаунт. Сборка клавиатуры в
aiogram квадратична (каждая кнопка копирует всю разметку): 1 000 кнопок —
8 с, 2 000 — 42 с, 10 000 — минуты. Всё это время стоял весь процесс: бот не
отвечал никому, мини-апп и фоновые циклы тоже. А Telegram такую клавиатуру и
такой текст всё равно бы не принял.

ОБРАТНЫЙ КОНТРОЛЬ (прогонялся): вернуть `for acc in shown` без страницы —
красные все три теста (на 2 000 аккаунтов тест падает по таймауту).
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytest.importorskip("aiogram")
pytest.importorskip("asyncpg")

from bot.handlers import accounts as A


class _Msg:
    def __init__(self):
        self.text = None
        self.markup = None

    async def edit_text(self, text, **kw):
        self.text, self.markup = text, kw.get("reply_markup")

    answer = edit_text


def _accs(n):
    return [{"id": i, "first_name": f"Акк{i}", "username": None, "phone": f"+7999{i:07d}",
             "status": "active", "is_active": True, "pool": None, "trust_score": None}
            for i in range(1, n + 1)]


def _run(monkeypatch, n, page=0, status_filter="all"):
    async def limit(pool, uid):
        return "pro", 0 if n == 0 else 100000

    async def get(pool, uid):
        return _accs(n)

    monkeypatch.setattr(A, "_get_account_limit", limit)
    monkeypatch.setattr(A.db, "get_tg_accounts", get)
    m = _Msg()
    t = time.perf_counter()
    asyncio.run(asyncio.wait_for(
        A._show_accounts_menu(m, None, 1, edit=True, status_filter=status_filter, page=page), 10))
    return m, time.perf_counter() - t


def _acc_buttons(m):
    return [b for row in m.markup.inline_keyboard for b in row
            if (b.callback_data or "").startswith("acc:view:")]


def test_big_fleet_menu_is_one_page(monkeypatch):
    m, dt = _run(monkeypatch, 2000)
    assert dt < 2, f"меню на 2 000 аккаунтов собиралось {dt:.1f} с — процесс стоит"
    assert len(_acc_buttons(m)) == A._ACC_MENU_PAGE
    assert len(m.text) < 4096
    assert "Страница 1 из 100" in m.text


def test_pages_walk_and_clamp(monkeypatch):
    m, _ = _run(monkeypatch, 45, page=1)
    ids = [b.callback_data for b in _acc_buttons(m)]
    assert ids[0] == "acc:view:21:0:0" and len(ids) == 20
    m, _ = _run(monkeypatch, 45, page=99)  # устаревшая кнопка после удаления
    assert len(_acc_buttons(m)) == 5 and "Страница 3 из 3" in m.text
    nav = [b.text for row in m.markup.inline_keyboard for b in row]
    assert "◀️" in nav and "▶️" not in nav


def test_small_fleet_has_no_pager(monkeypatch):
    m, _ = _run(monkeypatch, 5)
    assert len(_acc_buttons(m)) == 5
    assert "Страница" not in m.text
    assert not any(b.text == "▶️" for row in m.markup.inline_keyboard for b in row)

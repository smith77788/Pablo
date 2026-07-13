"""Паритет: глобальный поиск Telegram из бота (global_search).

Раньше поиск публичных сущностей (Telethon contacts.SearchRequest) был только
в mini-app. Добавлен бот-хендлер (/search) поверх того же движка
global_search_engine.search_public — инлайн, без очереди.
"""
from __future__ import annotations

import os

import tests.conftest  # noqa: F401 — ставит стабы telethon/aiogram

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_handler_module_imports_and_wires_router():
    import bot.handlers.global_search as gs
    assert gs.router is not None
    for fn in ("cmd_search", "cb_search_open", "cb_search_cancel", "msg_search_query"):
        assert hasattr(gs, fn), f"нет хендлера {fn}"


def test_uses_shared_search_engine():
    h = _read("bot/handlers/global_search.py")
    assert "global_search_engine as gse" in h
    assert "gse.search_public(" in h
    # запрос санитайзится тем же хелпером, что и в mini-app
    assert "sanitize_search_query(" in h


def test_picks_active_account_session_like_app():
    h = _read("bot/handlers/global_search.py")
    assert "is_active=TRUE AND session_str IS NOT NULL" in h
    assert "ORDER BY last_used DESC NULLS LAST LIMIT 1" in h


def test_router_registered_in_main():
    m = _read("main.py")
    assert "global_search as global_search_handler" in m
    assert "dp.include_router(global_search_handler.router)" in m


def test_search_command_present():
    h = _read("bot/handlers/global_search.py")
    assert 'Command("search")' in h


def test_backend_engine_exists():
    assert "async def search_public(" in _read("services/global_search_engine.py")

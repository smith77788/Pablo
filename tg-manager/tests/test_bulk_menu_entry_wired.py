"""Регрессия: 7 кнопок bulk-меню каналов больше не мёртвые.

Баг (аудит dead-buttons): кнопки bulk_dm / bulk_post / bulk_chan_uname /
bulk_chan_about / bulk_prof_name / bulk_prof_bio / bulk_prof_uname создавались в
bulk-меню, но НЕ имели callback-хендлера → молчаливый no-op. Вся downstream-логика
(_show_bulk_select → cb_bulk_confirm_selection → op) уже существовала; не хватало
проводки входа. Фикс: cb_bulk_menu_entry маршрутизирует эти 7 action → _show_bulk_select.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


BULK_ACTIONS = [
    "bulk_dm", "bulk_post", "bulk_chan_uname", "bulk_chan_about",
    "bulk_prof_name", "bulk_prof_bio", "bulk_prof_uname",
]


def test_bulk_menu_entry_handler_covers_all_seven():
    src = _read("bot/handlers/channel_ops.py")
    # единый хендлер входа существует и открывает выбор аккаунтов
    assert "async def cb_bulk_menu_entry" in src
    assert "_show_bulk_select(callback, pool, op, set())" in src
    # фильтр покрывает все 7 ранее-мёртвых action (статически, литеральным сетом)
    seg = src[src.index("_BULK_MENU_ENTRY = {"):src.index("async def cb_bulk_confirm_selection")]
    for a in BULK_ACTIONS:
        assert f'"{a}"' in seg, f"action {a} не подключён"
    # каждый action маппится на op-код, который confirm-сторона реально обрабатывает
    for op in ["dm", "post", "chan_uname", "chan_about", "prof_name", "prof_bio", "prof_uname"]:
        assert f'"{op}"' in seg


def test_confirm_side_handles_all_bulk_ops():
    src = _read("bot/handlers/channel_ops.py")
    conf = src[src.index("async def cb_bulk_confirm_selection"):]
    # downstream реально обрабатывает эти op (иначе вход вёл бы в никуда)
    for op in ["dm", "post", "chan_uname", "chan_about"]:
        assert f'"{op}"' in conf
    assert 'op in ("prof_name", "prof_bio", "prof_uname")' in conf

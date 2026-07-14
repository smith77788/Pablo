"""Паритет: Contacts Hub из бота (uch_*).

Единый реестр контактов (unified_contacts) со своими движками был доступен
только из mini-app. Бот получил /contacts: обзор, поиск, авто-теги, граф связей,
избранное — поверх тех же движков services/contacts_hub/*.
"""
from __future__ import annotations

import os

import tests.conftest  # noqa: F401 — стабы telethon/aiogram

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_handler_imports_and_router():
    import bot.handlers.contacts_hub as ch
    assert ch.router is not None
    for fn in ("cmd_contacts", "cb_menu", "cb_stats", "cb_smart_tags", "cb_graph",
               "cb_search", "msg_search_query", "cb_toggle_fav"):
        assert hasattr(ch, fn), f"нет хендлера {fn}"


def test_uses_real_engines():
    h = _read("bot/handlers/contacts_hub.py")
    assert "from services.contacts_hub.stats_engine import get_full_stats" in h
    assert "from services.contacts_hub.smart_tags_engine import apply_smart_tags" in h
    assert "from services.contacts_hub.relationship_engine import compute_relationships" in h
    assert "from services.contacts_hub.search_engine import search_contacts" in h
    assert "from services.contacts_hub.bulk_ops_engine import bulk_set_favorite" in h


def test_engines_exist_with_expected_signatures():
    assert "async def get_full_stats(" in _read("services/contacts_hub/stats_engine.py")
    assert "async def apply_smart_tags(" in _read("services/contacts_hub/smart_tags_engine.py")
    assert "async def compute_relationships(" in _read("services/contacts_hub/relationship_engine.py")
    assert "async def search_contacts(" in _read("services/contacts_hub/search_engine.py")
    assert "async def bulk_set_favorite(" in _read("services/contacts_hub/bulk_ops_engine.py")


def test_favorite_toggle_scoped_by_owner():
    h = _read("bot/handlers/contacts_hub.py")
    # чтение текущего состояния скоупится по owner_id (не даём трогать чужие)
    assert "WHERE id=$1 AND owner_id=$2" in h
    assert "bulk_set_favorite(pool, owner_id, [cid], not cur)" in h


def test_router_registered_in_main():
    m = _read("main.py")
    assert "contacts_hub as contacts_hub_handler" in m
    assert "dp.include_router(contacts_hub_handler.router)" in m


def test_command_present():
    assert 'Command("contacts")' in _read("bot/handlers/contacts_hub.py")


def test_callback_data_fits_telegram_limit():
    # chub:fav:<uuid36> должно влезать в лимит callback_data (64 байта)
    from bot.callbacks import ContactsHubCb
    packed = ContactsHubCb(action="fav", cid="550e8400-e29b-41d4-a716-446655440000").pack()
    assert len(packed.encode()) <= 64, f"callback_data слишком длинный: {packed}"

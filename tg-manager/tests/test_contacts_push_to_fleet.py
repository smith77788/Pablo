"""Наполнение контактных книг флота из хранилища (хранилище → книги, push).

Просьба владельца (18.11.2026): «аккаунты флота не синхронизируются со списком
контактов и не наполняют свою контактную книгу контактами из нашего хранилища».

Обратное направление к uch_sync (книги → хранилище). op contacts_push уже был, но
ставился ТОЛЬКО при подключении аккаунта (device_sync.schedule_for_new_account) —
ручной двери не было, и после пополнения хранилища книги флота не обновлялись.
Добавлены ручка /uch/push (на весь активный флот) и кнопка «📲 В книги флота».
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_push_route_registered():
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "POST /api/miniapp/uch/push" in snap


def test_push_endpoint_submits_contacts_push_for_whole_fleet():
    src = _src("services/mini_app_uch.py")
    body = src[src.index("async def uch_push("):]
    body = body[:body.index("async def uch_sync_center(")]
    # Это push (хранилище → книги), а не pull.
    assert '"contacts_push"' in body
    # На ВЕСЬ активный флот с сессией, а не на один аккаунт.
    assert '"account_ids": acc_ids' in body
    assert "is_active" in body and "session_str" in body
    # Нечего выгружать, если в хранилище нет контактов с номером — честный отказ.
    assert "phones" in body and "выгружать в книги флота нечего" in body


def test_frontend_has_push_button_and_function():
    html = _src("mini_app/index.html")
    assert "pushContactsToFleet()" in html
    assert "async function pushContactsToFleet(" in html
    assert "/api/miniapp/uch/push" in html
    # Кнопка в инструментах контактов.
    assert "📲 В книги флота" in html


def test_push_is_distinct_from_pull():
    """Ключевое: push и pull — разные направления и разные op-типы."""
    src = _src("services/mini_app_uch.py")
    push = src[src.index("async def uch_push("):src.index("async def uch_sync_center(")]
    pull = src[src.index("async def uch_sync("):src.index("async def uch_push(")]
    assert '"contacts_push"' in push and '"contacts_sync"' not in push
    assert '"contacts_sync"' in pull and '"contacts_push"' not in pull

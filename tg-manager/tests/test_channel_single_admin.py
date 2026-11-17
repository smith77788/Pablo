"""Карточка чата/канала: назначить ОДНОГО админа — аккаунт флота или @username.

Просьба владельца (18.11.2026): кнопка «Админы» делала админами весь флот; нужно
добавить одного — из списка флота или по юзернейму.
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_backend_endpoint_and_route():
    src = _src("services/mini_app_channels.py")
    assert "async def channel_promote_one(" in src
    body = src[src.index("async def channel_promote_one("):]
    body = body[:body.index("async def _submit_channel_manage(")]
    # Принимает аккаунт флота ИЛИ юзернейм.
    assert 'body.get("account_id")' in body and 'body.get("username")' in body
    # Повышает аккаунт-создатель (у него право «Назначать админов»).
    assert "acc_id" in body and "promote_to_admin_ex" in body
    # Аккаунт флота при необходимости вступает и повторяет.
    assert "join_channel_by_id" in body and "not_participant" in body
    snap = _src("tests/miniapp_routes_snapshot.txt")
    assert "POST /api/miniapp/channel/{ch_id}/promote_one" in snap


def test_username_resolver_exists():
    am = _src("services/account_manager.py")
    assert "async def resolve_user_id_by_username(" in am


def test_frontend_offers_fleet_account_or_username():
    html = _src("mini_app/index.html")
    fn = html[html.index("async function promoteChannel("):]
    fn = fn[:fn.index("async function promoteChannelAll(")]
    assert "Один аккаунт флота" in fn and "По @юзернейму" in fn and "Весь флот" in fn
    assert "_promoteOne(" in fn
    one = html[html.index("async function _promoteOne("):]
    one = one[:one.index("\n}\n", 1) + 2]
    assert "/promote_one" in one
    assert "account_id" in fn and "username" in fn

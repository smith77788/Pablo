"""Регрессия: единый диспетчер profile_setter_engine.apply_op.

Одна реализация op→движок для очереди (op_worker) и инлайн-пути (mini_app) —
без дублей. Проверяем, что каждый op маршрутизируется в правильную функцию и
что неизвестный op не падает, а возвращает {ok:False}.
"""
from __future__ import annotations

import inspect

import pytest

from services import profile_setter_engine as pse


@pytest.mark.asyncio
async def test_apply_op_routes_each_op(monkeypatch):
    called = {}

    def stub(name):
        async def _f(*a, **k):
            called["fn"] = name
            return {"ok": True, "fn": name}
        return _f

    for fn in ("set_name_bio", "set_avatar_from_url", "set_2fa_password", "set_username",
               "close_other_sessions", "set_privacy", "set_bio", "clear_bio", "remove_username",
               "remove_avatar", "reset_2fa", "set_online", "check_restriction"):
        monkeypatch.setattr(pse, fn, stub(fn))
    monkeypatch.setattr(pse, "expand_spintax", lambda s: s)

    cases = {
        "name": "set_name_bio", "avatar": "set_avatar_from_url", "2fa": "set_2fa_password",
        "username": "set_username", "bio": "set_bio", "close_sessions": "close_other_sessions",
        "privacy": "set_privacy", "clear_bio": "clear_bio", "remove_username": "remove_username",
        "remove_avatar": "remove_avatar", "reset_2fa": "reset_2fa", "set_online": "set_online",
        "check_restriction": "check_restriction",
    }
    for op, expected_fn in cases.items():
        called.clear()
        res = await pse.apply_op("sess", {"id": 1}, op, {"name_data": {}, "avatar_url": "u",
                                                          "username": "x", "about": "hi",
                                                          "privacy_key": "phone"})
        assert res.get("ok"), op
        assert called.get("fn") == expected_fn, f"{op} → {called.get('fn')} (ждали {expected_fn})"


def test_set_bio_only_touches_about_not_name():
    # «Установить bio» должен менять ТОЛЬКО about — не затирать имя/фамилию
    # (в отличие от set_name_bio, где пустой last_name сбрасывал бы фамилию).
    src = inspect.getsource(pse.set_bio)
    assert "about" in src
    assert "first_name" not in src, "set_bio не должен трогать first_name"
    assert "last_name" not in src, "set_bio не должен трогать last_name"


@pytest.mark.asyncio
async def test_apply_op_unknown_returns_error():
    res = await pse.apply_op("sess", {"id": 1}, "nonexistent_op", {})
    assert res["ok"] is False and "unknown op" in res["error"]


def test_executor_uses_shared_dispatcher_not_duplicate():
    from services import op_worker
    src = inspect.getsource(op_worker._exec_bulk_set_profile)
    assert "apply_op(" in src, "executor должен использовать единый pse.apply_op"
    assert "pse.set_name_bio" not in src, "в executor остался дублирующий диспетчер op→движок"

"""Массовое обновление профилей: флуд и причины ошибок по-русски (регресс).

Было: update_profile пробрасывает FloodWaitError, исполнитель ловил его общим
except и показывал владельцу английское «A wait of 300 seconds is required»,
а флуд аккаунта никуда не записывался. Ошибки username («The username is
already taken …») тоже шли по-английски, а «имя не изменилось» считалось
ошибкой.
"""
from __future__ import annotations

import asyncio

from services import op_worker as ow


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required (caused by UpdateProfileRequest)")
        self.seconds = seconds


def _run(monkeypatch, field, behaviour):
    accs = [{"id": 1, "first_name": "Аня", "session_str": "s"},
            {"id": 2, "first_name": "Боря", "session_str": "s"}]
    floods: list = []

    async def _sel(*a, **k):
        return accs

    async def _claim(ids):
        return list(ids)

    async def _noop(*a, **k):
        return None

    async def _false(*a, **k):
        return False

    async def _rec(pool, acc_id, secs, *a, **k):
        floods.append((acc_id, secs))

    from services import account_manager as am
    from services import flood_engine as fe

    monkeypatch.setattr(ow.resource_selector, "select_all_active", _sel)
    monkeypatch.setattr(ow, "try_claim_accounts", _claim)
    monkeypatch.setattr(ow, "release_accounts", _noop)
    monkeypatch.setattr(ow, "_safe_execute", _noop)
    monkeypatch.setattr(ow, "_is_cancelled", _false)
    monkeypatch.setattr(fe, "record_flood", _rec)
    monkeypatch.setattr(am, "update_profile", behaviour)
    monkeypatch.setattr(am, "update_account_username", behaviour)
    monkeypatch.setattr(ow.asyncio, "sleep", _noop)
    res = asyncio.new_event_loop().run_until_complete(ow._exec_bulk_update_profile(
        None, None, 7, 100, {"account_ids": [1, 2], "field": field, "value": "v"}))
    return res, floods


def test_flood_is_recorded_and_explained_in_russian(monkeypatch):
    async def beh(sess, _acc=None, **kw):
        if _acc["id"] == 1:
            raise FloodWaitError(300)
        return True

    res, floods = _run(monkeypatch, "about", beh)
    assert floods == [(1, 300)]
    assert res["ok"] == 1 and res["failed"] == 1
    assert "A wait of" not in res["summary"]
    assert "подождать 300 с" in res["summary"]


def test_username_errors_in_russian_and_not_modified_is_ok(monkeypatch):
    async def beh(sess, username, _acc=None):
        if _acc["id"] == 1:
            return "The username is already taken (caused by UpdateUsernameRequest)"
        return "The username is not different from the current username (caused by UpdateUsernameRequest)"

    # «не отличается» Telegram отдаёт как USERNAME_NOT_MODIFIED
    async def beh2(sess, username, _acc=None):
        if _acc["id"] == 1:
            return "The username is already taken (caused by UpdateUsernameRequest)"
        return "USERNAME_NOT_MODIFIED"

    res, _ = _run(monkeypatch, "username", beh2)
    assert "уже занято" in res["summary"] and "already taken" not in res["summary"]
    assert res["ok"] == 1 and res["failed"] == 1


def test_profile_error_ru_pure():
    assert ow._profile_error_ru("FloodWait 42с — подождите")[1] == 42
    assert ow._profile_error_ru("USERNAME_INVALID")[0].startswith("недопустимое")
    assert ow._profile_error_ru("")[0] == "ошибка"

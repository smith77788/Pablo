"""Боты с новыми суффиксами username'а (app/ai/agent/game, Telegram 2026)."""
import asyncio

import asyncpg
import pytest

from services import bot_usernames as bu
from services import managed_bots as mb
from services.account_manager import parse_mybots_usernames


@pytest.mark.parametrize("u,suf", [
    ("shopapp", "app"), ("Shop_AI", "ai"), ("my_agent", "agent"),
    ("fungame", "game"), ("someGay", "gay"),
    ("mainaibot", None), ("shop_bot", None), ("ai", None), ("", None),
])
def test_extra_suffix_of(u, suf):
    assert bu.extra_suffix_of(u) == suf


@pytest.mark.parametrize("u", ["@shopapp", "t.me/my_ai", "https://t.me/MyGame?start=1",
                               "fun_agent"])
def test_validate_extra_accepts_and_normalizes(u):
    ok, norm = bu.validate_extra(u)
    assert ok, norm
    assert "@" not in norm and "/" not in norm


@pytest.mark.parametrize("u,needle", [
    ("shopbot", "основной"), ("shopxyz", "оканчиваться"), ("1shopapp", "латиницу"),
    ("app", "5–32"), ("a" * 30 + "app", "5–32"), ("", "пустой"), ("shop-app", "латиницу"),
])
def test_validate_extra_rejects(u, needle):
    ok, err = bu.validate_extra(u)
    assert not ok and needle in err


def test_main_username_still_requires_bot_with_clear_hint():
    assert mb.validate_bot_username("@shop_bot") == (True, "shop_bot")
    ok, err = mb.validate_bot_username("shopapp")
    assert not ok and "дополнительным" in err
    ok, err = mb.validate_bot_username("shopxyz")
    assert not ok and "bot" in err
    with pytest.raises(ValueError):
        mb.build_creation_link("mgr", "shopapp")


def test_is_bot_username():
    assert bu.is_bot_username("@ShopBot") and bu.is_bot_username("shopapp")
    assert not bu.is_bot_username("Back") and not bu.is_bot_username("shopxyz")


def test_mybots_parser_keeps_suffix_usernames():
    got = parse_mybots_usernames(["@shop_bot", "@shopapp", "@Fun_Game", "« Back", "@shopxyz"])
    assert got == ["shop_bot", "shopapp", "Fun_Game"]


class _Pool:
    """Минимальный пул: одна строка managed_bots и опционально «чужой» бот."""

    def __init__(self, row=None, taken=None, unique_violation=False):
        self.row, self.taken, self.uv = row, taken, unique_violation
        self.updates = []

    async def fetchrow(self, sql, *a):
        return self.row

    async def fetchval(self, sql, *a):
        return self.taken

    async def execute(self, sql, *a):
        if self.uv and "extra_username=$3" in sql:
            raise asyncpg.UniqueViolationError("dup")
        self.updates.append(a)
        return "UPDATE 1"


def _row(**kw):
    return {"bot_id": 42, "username": "shop_bot", "extra_username": None, **kw}


def _resolver(result):
    async def r(pool, owner_id, username):
        r.called = username
        return result
    r.called = None
    return r


def _run(coro):
    return asyncio.run(coro)


def test_set_extra_verified():
    pool = _Pool(_row())
    res = _run(mb.set_extra_username(pool, 7, 42, "@ShopApp",
                                     resolver=_resolver({"status": "found", "user_id": 42})))
    assert res == {"ok": True, "extra_username": "ShopApp", "verified": True, "hint": ""}
    assert pool.updates[-1] == (42, 7, "ShopApp", True)


def test_set_extra_unverified_on_flood_still_saves():
    pool = _Pool(_row())
    res = _run(mb.set_extra_username(pool, 7, 42, "shopai", resolver=_resolver(
        {"status": "unverified", "reason": "flood_wait"})))
    assert res["ok"] and res["verified"] is False and "ограничил" in res["hint"]
    assert pool.updates[-1][3] is False


def test_set_extra_rejects_foreign_and_free_usernames():
    pool = _Pool(_row())
    res = _run(mb.set_extra_username(pool, 7, 42, "shopai", resolver=_resolver(
        {"status": "found", "user_id": 999})))
    assert not res["ok"] and res["status"] == 409
    res = _run(mb.set_extra_username(pool, 7, 42, "shopai", resolver=_resolver(
        {"status": "not_found"})))
    assert not res["ok"] and res["status"] == 400 and "BotFather" in res["error"]
    assert pool.updates == []


def test_set_extra_validation_before_network():
    pool = _Pool(_row())
    r = _resolver({"status": "found", "user_id": 42})
    res = _run(mb.set_extra_username(pool, 7, 42, "shop_bot2bot", resolver=r))
    assert res["status"] == 400 and r.called is None


def test_set_extra_not_owner_and_taken():
    assert _run(mb.set_extra_username(_Pool(None), 7, 42, "shopapp",
                                      resolver=_resolver({})))["status"] == 404
    res = _run(mb.set_extra_username(_Pool(_row(), taken=5), 7, 42, "shopapp",
                                     resolver=_resolver({})))
    assert res["status"] == 409
    res = _run(mb.set_extra_username(_Pool(_row(), unique_violation=True), 7, 42, "shopapp",
                                     resolver=_resolver({"status": "found", "user_id": 42})))
    assert res["status"] == 409


def test_clear_extra():
    pool = _Pool(_row(extra_username="shopapp"))
    res = _run(mb.set_extra_username(pool, 7, 42, "  ", resolver=_resolver({})))
    assert res["ok"] and res["extra_username"] is None and pool.updates

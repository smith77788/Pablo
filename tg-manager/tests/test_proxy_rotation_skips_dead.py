"""Ротация не должна селить аккаунты на подтверждённо мёртвые прокси.

Активный баг: пул ротации собирался как «прокси владельца, is_active=TRUE» —
без учёта живости. Пока фоновой проверки не существовало, is_alive почти
всегда был NULL и вреда не приносил. Но сторож прокси теперь реально ведёт это
поле, и без фикса «Ротация IP» и «Назначить прокси» переселяли бы здоровые
аккаунты на прокси, о смерти которых система уже знает, — то есть делали ровно
обратное тому, ради чего их запускают.
"""
from __future__ import annotations

import asyncio
import pathlib

from services.proxy_rotation import apply_rotation

_ROOT = pathlib.Path(__file__).resolve().parent.parent


class _Conn:
    def __init__(self, accounts, proxies, blocked=()):
        self._accounts = accounts
        self._proxies = proxies
        self._blocked = list(blocked)
        self.updates = []

    async def fetch(self, q, *a):
        if "FROM tg_accounts" in q and "FOR UPDATE" in q:
            return self._accounts
        if "FROM user_proxies" in q:
            return self._proxies
        if "DISTINCT proxy_id" in q:
            return self._blocked
        return []

    async def execute(self, q, *a):
        self.updates.append(a)
        return "UPDATE 1"

    def transaction(self):
        return _Ctx()


class _Ctx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *e):
        return False


class _Acquire:
    def __init__(self, conn):
        self._c = conn

    async def __aenter__(self):
        return self._c

    async def __aexit__(self, *e):
        return False


class _Pool:
    def __init__(self, conn):
        self._c = conn

    def acquire(self):
        return _Acquire(self._c)


def _acc(i, proxy_id=None, busy=False):
    return {"id": i, "proxy_id": proxy_id, "busy": busy}


def _prx(i, alive):
    return {"id": i, "is_alive": alive}


def test_dead_proxies_are_excluded_from_the_pool():
    conn = _Conn([_acc(1)], [_prx(10, False), _prx(11, True)])
    res = asyncio.run(apply_rotation(_Pool(conn), owner_id=1))
    assert res["skipped_dead"] == 1
    assigned = [u[0] for u in conn.updates]
    assert 10 not in assigned, "аккаунт нельзя селить на мёртвый прокси"
    assert assigned == [11]


def test_unchecked_proxies_stay_usable():
    """is_alive=NULL значит «ещё не проверяли». Исключать их нельзя — на свежем
    флоте, где проверок не было, пул оказался бы пуст."""
    conn = _Conn([_acc(1)], [_prx(10, None)])
    res = asyncio.run(apply_rotation(_Pool(conn), owner_id=1))
    assert res["skipped_dead"] == 0
    assert [u[0] for u in conn.updates] == [10]


def test_all_dead_leaves_nothing_to_assign():
    conn = _Conn([_acc(1)], [_prx(10, False), _prx(11, False)])
    res = asyncio.run(apply_rotation(_Pool(conn), owner_id=1))
    assert conn.updates == []
    assert res["skipped_dead"] == 2
    assert res["skipped_no_proxy"] >= 1, "нехватка обязана быть отражена честно"


def test_account_is_not_moved_onto_a_dead_proxy_from_a_working_one():
    """Самый вредный случай: рабочий аккаунт переселяют на мёртвый прокси."""
    conn = _Conn([_acc(1, proxy_id=11)], [_prx(10, False), _prx(11, True)])
    asyncio.run(apply_rotation(_Pool(conn), owner_id=1))
    assert all(u[0] != 10 for u in conn.updates)


def test_explicit_pool_selection_also_filters_dead():
    conn = _Conn([_acc(1)], [_prx(10, False)])
    res = asyncio.run(apply_rotation(_Pool(conn), owner_id=1, proxy_ids=[10]))
    assert conn.updates == [] and res["skipped_dead"] == 1


def test_busy_accounts_still_skipped():
    """Прежняя защита не должна пострадать от изменения пула."""
    conn = _Conn([_acc(1, busy=True)], [_prx(10, True)])
    res = asyncio.run(apply_rotation(_Pool(conn), owner_id=1))
    assert res["skipped_busy"] == 1 and conn.updates == []


# ── Причина доведена до пользователя ──────────────────────────────────────────

def test_reason_is_reported_in_both_frontends():
    """«Не хватило прокси» при полном списке выглядит беспричинным."""
    api = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
    ui = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
    bot = (_ROOT / "bot" / "handlers" / "proxy_manager.py").read_text(encoding="utf-8")
    assert "skipped_dead" in api and "skipped_dead" in ui and "skipped_dead" in bot


def test_pool_filter_is_strict_not_falsy():
    """NULL («не проверяли») не должен считаться мёртвым — иначе на свежем
    флоте пул схлопнется в ноль."""
    src = (_ROOT / "services" / "proxy_rotation.py").read_text(encoding="utf-8")
    assert "is not False" in src and "is False" in src
    assert "if not _alive" not in src, "нестрогая проверка выбросила бы непроверенные прокси"

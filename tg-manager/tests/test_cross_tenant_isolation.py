"""Изоляция пользователей: чужой номер в запросе не даёт доступа к чужим данным.

Три дыры, которые закрывает этот набор.

1. Workspace открывался по одному номеру. `create_workspace_invite` выписывал
   код-приглашение кому угодно на любой `ws_id`: посторонний выпускал себе код
   в чужой workspace и входил в него участником. Рядом `get_workspace` и
   `get_workspace_members` отдавали название, описание и весь список участников
   (чужие user_id и имена) без проверки членства.

2. Заказ продвижения создавался с ЧУЖИМ `smm_panel_id`. Дальше запуск накрутки
   проверял владельца только у самого заказа, а панель доставал без скоупа — и
   уходил в SMM-панель с чужим API-ключом, за чужой счёт. То же с ботом склада,
   строка которого содержит bot_token_enc.

3. Общая предпосылка обеих дыр: идентификаторы приходят из данных кнопки или
   тела запроса, то есть от клиента. Данные кнопки отправляет клиент, а не
   Telegram, и у каждого пользователя этого продукта есть MTProto-клиент.

Тесты проверяют САМ SQL: в запрос обязан уходить owner_id/viewer_id. Проверять
результат на заглушке бессмысленно — заглушка вернёт что угодно, а настоящий
фильтр живёт именно в тексте запроса и его аргументах.
"""
from __future__ import annotations

import asyncio

import pytest

from database import db


def _run(coro):
    return asyncio.run(coro)


class _Pool:
    """Пул-заглушка: запоминает запросы и отдаёт заранее заданные ответы."""

    def __init__(self, fetchrow=None, fetchval=None, fetch=None):
        self.calls: list[tuple[str, tuple]] = []
        self._fetchrow = fetchrow
        self._fetchval = fetchval
        self._fetch = fetch

    async def fetchrow(self, q, *a):
        self.calls.append((q, a))
        return self._fetchrow

    async def fetchval(self, q, *a):
        self.calls.append((q, a))
        return self._fetchval

    async def fetch(self, q, *a):
        self.calls.append((q, a))
        return self._fetch or []

    async def execute(self, q, *a):
        self.calls.append((q, a))
        return "INSERT 0 1"


def _sql(pool) -> str:
    return " ".join(" ".join(q.split()) for q, _ in pool.calls)


# ── Workspaces ────────────────────────────────────────────────────────────────

def test_invite_refused_for_non_member():
    """Посторонний не получает код-приглашения в чужой workspace."""
    pool = _Pool(fetchval=None)  # роли нет — не участник
    code = _run(db.create_workspace_invite(pool, ws_id=42, created_by=999))
    assert code is None
    assert "INSERT INTO workspace_invites" not in _sql(pool)


def test_invite_refused_for_plain_member():
    """Участник без прав администратора тоже не приглашает."""
    for role in ("member", "viewer"):
        pool = _Pool(fetchval=role)
        assert _run(db.create_workspace_invite(pool, ws_id=42, created_by=7)) is None
        assert "INSERT INTO workspace_invites" not in _sql(pool)


def test_invite_allowed_for_owner_and_admin():
    """Владелец и админ приглашать могут — иначе фикс ломает саму функцию."""
    for role in db.WORKSPACE_INVITE_ROLES:
        pool = _Pool(fetchval=role)
        code = _run(db.create_workspace_invite(pool, ws_id=42, created_by=7))
        assert code, f"роль {role} обязана уметь приглашать"
        assert "INSERT INTO workspace_invites" in _sql(pool)


def test_get_workspace_with_viewer_joins_members():
    """С viewer_id запрос обязан соединяться с workspace_members по этому id."""
    pool = _Pool(fetchrow=None)
    _run(db.get_workspace(pool, ws_id=42, viewer_id=777))
    sql, args = pool.calls[0]
    flat = " ".join(sql.split())
    assert "workspace_members" in flat, "членство не проверяется"
    assert 777 in args


def test_get_workspace_members_empty_for_outsider():
    """Список участников постороннему не достаётся вовсе."""
    pool = _Pool(fetchval=None, fetch=[{"user_id": 1}])
    members = _run(db.get_workspace_members(pool, ws_id=42, viewer_id=999))
    assert members == []
    assert "FROM workspace_members wm" not in _sql(pool) or "pu.username" not in _sql(pool)


def test_get_workspace_members_visible_to_member():
    pool = _Pool(fetchval="member", fetch=[{"user_id": 1}])
    members = _run(db.get_workspace_members(pool, ws_id=42, viewer_id=1))
    assert members == [{"user_id": 1}]


def test_workspace_role_checks_active_workspace():
    """Роль в выключенном workspace не считается доступом."""
    pool = _Pool(fetchval="owner")
    _run(db.get_workspace_role(pool, ws_id=42, user_id=7))
    sql = _sql(pool)
    assert "is_active" in sql.lower()


# ── Промо: SMM-панель и бот склада ────────────────────────────────────────────

@pytest.mark.parametrize(
    "fn, kwargs, table",
    [
        (db.smm_get_panel, {"panel_id": 5}, "smm_panels"),
        (db.warehouse_get_bot, {"bot_id": 5}, "bot_warehouse"),
        (db.promo_get_order, {"order_id": 5}, "promo_orders"),
    ],
)
def test_getters_scope_by_owner_when_asked(fn, kwargs, table):
    pool = _Pool(fetchrow=None)
    _run(fn(pool, owner_id=1234, **kwargs))
    sql, args = pool.calls[0]
    flat = " ".join(sql.split())
    assert table in flat
    assert "owner_id=$2" in flat, f"{fn.__name__} не фильтрует по владельцу: {flat}"
    assert args == (5, 1234)


@pytest.mark.parametrize(
    "fn, kwargs",
    [
        (db.smm_get_panel, {"panel_id": 5}),
        (db.warehouse_get_bot, {"bot_id": 5}),
        (db.promo_get_order, {"order_id": 5}),
    ],
)
def test_getters_still_work_without_owner_for_background(fn, kwargs):
    """Фоновым обходам (планировщик) владелец не известен — режим остаётся."""
    pool = _Pool(fetchrow={"id": 5})
    assert _run(fn(pool, **kwargs)) == {"id": 5}
    assert "owner_id" not in _sql(pool)


def test_order_with_foreign_panel_is_refused():
    """Заказ с чужой панелью не создаётся — проверка на записи, не на чтении."""
    pool = _Pool(fetchrow=None)  # панель с таким id у этого владельца не найдена
    with pytest.raises(ValueError):
        _run(db.promo_create_order(pool, owner_id=1, keyword="к", smm_panel_id=99))
    assert "INSERT INTO promo_orders" not in _sql(pool)


def test_order_with_foreign_bot_is_refused():
    pool = _Pool(fetchrow=None)
    with pytest.raises(ValueError):
        _run(db.promo_create_order(pool, owner_id=1, keyword="к", bot_id=99))
    assert "INSERT INTO promo_orders" not in _sql(pool)


def test_order_without_refs_is_created():
    """Заказ без бота и панели проверять нечего — он должен создаваться."""
    pool = _Pool(fetchrow={"id": 77})
    assert _run(db.promo_create_order(pool, owner_id=1, keyword="к")) == 77
    assert "INSERT INTO promo_orders" in _sql(pool)


def test_order_with_own_refs_is_created():
    pool = _Pool(fetchrow={"id": 77})
    got = _run(db.promo_create_order(
        pool, owner_id=1, keyword="к", bot_id=5, smm_panel_id=6,
    ))
    assert got == 77
    # обе проверки принадлежности прошли по owner_id владельца заказа
    owner_args = [a for q, a in pool.calls if "owner_id=$2" in " ".join(q.split())]
    assert len(owner_args) == 2
    assert all(a[1] == 1 for a in owner_args)

"""В свою экосистему нельзя записать чужой канал, бота или аккаунт.

Доступ к объектам выдаётся в том числе «через экосистему». Список каналов
показывает канал, если он лежит в экосистеме, которой владеет спрашивающий
ИЛИ в которой есть хоть один его объект (services/mini_app_api, запрос
channels). `database.db.get_bot` устроен так же — и возвращает бота вместе с
РАСШИФРОВАННЫМ токеном.

Запись в экосистему шла через ecosystem_brain.add_member, и она принимала
любой object_id. В боте этот id приходит прямо из callback_data
(cb_ecopick_add), то есть целиком со стороны клиента, а у каждого
пользователя этого продукта есть свой MTProto-клиент и он может отправить
любой callback_data. Достаточно было дописать чужой channel_id в СВОЮ
экосистему — и чужой канал появлялся в своём списке; для бота это означало
ещё и его токен.

Проверка намеренно «не чужое», а не «точно моё»: объекты часто добавляются в
экосистему в момент создания, когда строки о них ещё нет в базе. Существующий
объект чужого владельца — отказ; несуществующий — пропускаем.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

from services import ecosystem_brain as EB

VICTIM, ATTACKER = 111, 222


class _Pool:
    """Канал 500 принадлежит VICTIM, канал 900 не существует."""

    def __init__(self, rows: dict[tuple[str, int], int]):
        self._rows = rows          # (таблица, id) -> владелец
        self.inserts: list[str] = []

    async def fetchrow(self, sql, *args):
        table = next(t for t in ("tg_accounts", "managed_bots", "managed_channels")
                     if t in sql)
        oid, uid = int(args[0]), int(args[1])
        owner = self._rows.get((table, oid))
        return {"found": owner is not None, "mine": owner == uid}

    async def execute(self, sql, *args):
        # Запись отказа в журнал (operation_audit) — не добавление в
        # экосистему; считаем только то, что реально кладёт объект.
        if "operation_audit" not in sql:
            self.inserts.append(sql)
        return "INSERT 0 1"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _pool():
    return _Pool({
        ("managed_channels", 500): VICTIM,
        ("managed_bots", 700): VICTIM,
        ("tg_accounts", 800): VICTIM,
    })


@pytest.mark.parametrize("obj_type,obj_id", [
    ("channel", 500), ("group", 500), ("bot", 700), ("account", 800),
])
def test_foreign_object_is_not_added(obj_type, obj_id):
    pool = _pool()
    ok = _run(EB.add_member(pool, 1, ATTACKER, obj_type, obj_id))

    assert ok is False
    assert not pool.inserts, (
        f"чужой {obj_type} записан в экосистему — он станет виден постороннему"
    )


def test_own_object_is_still_added():
    pool = _pool()
    ok = _run(EB.add_member(pool, 1, VICTIM, "channel", 500))

    assert ok is True
    assert pool.inserts, "свой канал обязан добавляться как раньше"


def test_object_that_does_not_exist_yet_is_allowed():
    """Канал добавляют в экосистему в момент создания — строки ещё нет."""
    pool = _pool()
    ok = _run(EB.add_member(pool, 1, ATTACKER, "channel", 900))

    assert ok is True
    assert pool.inserts, "создание канала не должно ломаться из-за проверки"


def test_unknown_object_type_does_not_crash():
    pool = _pool()
    assert _run(EB.add_member(pool, 1, ATTACKER, "нечто", 1)) is True


def test_failed_check_does_not_let_the_object_through():
    """Гейт доступа при сбое проверки закрывается, а не открывается."""

    class _Broken(_Pool):
        async def fetchrow(self, sql, *args):
            raise RuntimeError("нет связи с базой")

    pool = _Broken({})
    assert _run(EB.add_member(pool, 1, ATTACKER, "channel", 500)) is False
    assert not pool.inserts


def test_bot_handler_refuses_before_adding():
    """Кнопка в боте обязана проверять владение сама и говорить причину."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "bot", "handlers", "ecosystems.py",
    )
    src = open(path, encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "cb_ecopick_add")
    calls = [c.func.attr for c in ast.walk(fn)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)]
    assert "object_belongs_to_someone_else" in calls, (
        "object_id приходит из callback_data — его владение надо проверять"
    )
    body = ast.unparse(fn)
    i_check = body.index("object_belongs_to_someone_else")
    i_add = body.index("add_member")
    assert i_check < i_add, "проверка обязана идти до добавления"

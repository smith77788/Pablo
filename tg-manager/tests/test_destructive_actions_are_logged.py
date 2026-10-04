"""Разрушительные действия и раздача прав обязаны попадать в журнал.

`operation_audit` отвечает на вопрос «куда делись сорок аккаунтов» и «откуда
этот человек взялся в моём пространстве». Отвечает он только на то, что в него
записали, а доступ к ресурсам в этом продукте даётся не только хозяину: и
workspace, и экосистема пускают к чужим ботам, каналам и аккаунтам. Значит
удалить ресурс или снять предохранитель может не один человек, и без записи
разбор инцидента упирается в пустоту.

Список ниже — намеренно явный, а не «любой DELETE». Удаление шаблона, воронки
или ключевого слова — правка настройки, её журналить незачем; здесь только
ресурсы (аккаунты, боты, каналы, прокси, сессии), права (пространства,
экосистемы) и снятие предохранителя (риск аккаунта). Добавили новое действие
этого класса — допишите его сюда вместе с записью в журнал.
"""
from __future__ import annotations

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# файл -> имена функций, которые обязаны писать в operation_audit
_ОБЯЗАНЫ = {
    "database/db.py": [
        "remove_tg_account",          # удаление аккаунта вместе с сессией
        "delete_bot",                 # удаление бота
        "use_workspace_invite",       # вход в чужое пространство — выдача прав
        "delete_workspace_member",    # выход/исключение — снятие прав
    ],
    "services/mini_app_api.py": [
        "account_delete",
        "accounts_mass",              # массовое удаление аккаунтов
        "channel_remove",
        "leave_workspace",            # выход участника и удаление пространства
        "proxy_cleanup_dead",         # массовое удаление прокси
        "presence_pack_delete",
        "ecosystem_delete",
    ],
    "services/account_reset.py": [
        "reset_account",              # снятие риска = выключение карантина
        "reset_all_cooled",
    ],
}


def _исходники(rel: str) -> dict[str, str]:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    out: dict[str, str] = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            out[node.name] = "\n".join(lines[node.lineno - 1:node.end_lineno])
    return out


def _пишет_в_журнал(имя: str, тела: dict[str, str]) -> bool:
    """Прямой вызов record_manual_action или вызов соседней функции, которая его делает.

    Один уровень косвенности нужен: там, где запись одинаковая для нескольких
    путей (сброс риска поодиночке и массово), она вынесена в общий хелпер
    рядом, и требовать именно прямой вызов значило бы запрещать это.
    """
    тело = тела[имя]
    if "record_manual_action" in тело:
        return True
    return any(
        сосед != имя and сосед in тело and "record_manual_action" in соседнее_тело
        for сосед, соседнее_тело in тела.items()
    )


@pytest.mark.parametrize("rel", sorted(_ОБЯЗАНЫ))
def test_разрушительное_действие_пишет_в_журнал(rel):
    тела = _исходники(rel)
    пропущены = []
    for имя in _ОБЯЗАНЫ[rel]:
        assert имя in тела, f"{rel}: функция {имя} не найдена — её переименовали?"
        if not _пишет_в_журнал(имя, тела):
            пропущены.append(имя)
    assert not пропущены, (
        f"{rel}: эти действия больше не пишут в operation_audit: {пропущены}. "
        "Журнал — единственный ответ на вопрос «кто это сделал»."
    )


class _Пул:
    """Пул-заглушка: помнит записи в operation_audit."""

    def __init__(self, deleted: str = "DELETE 1"):
        self._deleted = deleted
        self.аудит: list[tuple] = []

    async def execute(self, q, *a):
        if "INSERT INTO operation_audit" in q:
            self.аудит.append(a)
            return "INSERT 0 1"
        return self._deleted


@pytest.mark.asyncio
async def test_выход_из_пространства_реально_пишет_запись():
    from database import db

    pool = _Пул()
    await db.delete_workspace_member(pool, 10, 42)
    assert pool.аудит, "запись о выходе из пространства не появилась"
    owner_id, action, target, result, _err = pool.аудит[0]
    assert action == "workspace_member_leave"
    assert "ws:10" in target and "user:42" in target
    assert owner_id == 42


@pytest.mark.asyncio
async def test_ничего_не_удалилось_запись_не_появляется():
    """Нажали «выйти» второй раз — строки уже нет, событию взяться неоткуда."""
    from database import db

    pool = _Пул(deleted="DELETE 0")
    await db.delete_workspace_member(pool, 10, 42)
    assert pool.аудит == []


@pytest.mark.asyncio
async def test_снятие_риска_пишет_запись():
    """`risk_cleared_at` выключает карантин — это событие для разбора банов."""
    from services import account_reset

    pool = _Пул(deleted="UPDATE 1")
    assert await account_reset.reset_account(pool, 7, 42) is True
    assert pool.аудит, "снятие риска прошло мимо журнала"
    owner_id, action, target, *_ = pool.аудит[0]
    assert (owner_id, action, target) == (42, "account_risk_cleared", "7")


@pytest.mark.asyncio
async def test_чужой_аккаунт_не_сброшен_и_в_журнал_не_попал():
    from services import account_reset

    pool = _Пул(deleted="UPDATE 0")
    assert await account_reset.reset_account(pool, 7, 42) is False
    assert pool.аудит == []

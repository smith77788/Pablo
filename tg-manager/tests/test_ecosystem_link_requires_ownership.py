"""Привязать объект к экосистеме можно только к своей.

Экосистема — структура доступа: списки ботов и каналов пускают «через
экосистему» (`_BOTS_VISIBLE_SQL`, `_CHANNELS_VISIBLE_SQL`), поэтому запись в
неё — это изменение прав, а не пометка. Номер экосистемы в двух привязках
(регистрация бота и план глобального присутствия) приходит из ТЕЛА запроса,
то есть полностью со стороны клиента, и проверки владения там не было:
посторонний дописывал свой объект в чужую экосистему, и тот появлялся в
списках её владельца.

Остальные двери экосистем — и в мини-аппе, и в боте — владение проверяли
(`ecosystems WHERE id=$1 AND owner_id=$2`, `get_ecosystem(pool, eco_id,
owner)`), эти две выбивались.
"""
from __future__ import annotations

import ast
import asyncio
import os

import pytest

from services import mini_app_api as M

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = 4242


class _Пул:
    def __init__(self, *, своя: bool = True, падать: bool = False):
        self._своя = своя
        self._падать = падать

    async def fetchval(self, q, *a):
        if self._падать:
            raise RuntimeError("база недоступна")
        if "FROM ecosystems" in q:
            return 1 if self._своя else 0
        return 0


def _вызов(pool, eco_id):
    return asyncio.run(M._ecosystem_is_mine(pool, eco_id, UID))


def test_своя_экосистема_проходит():
    assert _вызов(_Пул(своя=True), 7) is True


def test_чужая_экосистема_не_проходит():
    assert _вызов(_Пул(своя=False), 7) is False


def test_сбой_базы_закрывает_доступ():
    """Гейт прав при ошибке обязан закрываться, а не открываться."""
    assert _вызов(_Пул(падать=True), 7) is False


@pytest.mark.parametrize("мусор", [None, "", "абв", {"id": 1}])
def test_нечисловой_номер_не_проходит(мусор):
    assert _вызов(_Пул(своя=True), мусор) is False


def _функции(rel: str) -> dict[str, str]:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    return {
        node.name: "\n".join(lines[node.lineno - 1:node.end_lineno])
        for node in ast.walk(ast.parse(src))
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }


def test_каждая_привязка_к_экосистеме_проверяет_владение():
    """Свип: новая привязка без проверки прав дальше этого теста не пройдёт."""
    тела = _функции("services/mini_app_api.py")
    без_проверки = []
    for имя, тело in тела.items():
        if имя == "setup_routes":
            continue  # обёртка: внутри неё лежат сами хендлеры, их и проверяем
        if "INSERT INTO ecosystem_" not in тело:
            continue
        if "INSERT INTO ecosystems(" in тело or "INSERT INTO ecosystems (" in тело:
            continue  # создание СВОЕЙ экосистемы — владельцем становится автор
        if "_ecosystem_is_mine" in тело or "owner_id=$2" in тело:
            continue
        без_проверки.append(имя)
    assert not без_проверки, (
        f"привязка к экосистеме без проверки владения: {без_проверки}. "
        "Номер экосистемы приходит от клиента, запись в чужую — выдача прав."
    )

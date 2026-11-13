"""Быстрая уборка мёртвых сущностей после проверки ограничений.

Владелец: «после проверки каналов/чатов/ботов не могу быстро удалить те, что
Telegram уже заблокировал/удалил». Добавлены ручки
/api/miniapp/restrictions/dead и /remove_dead и секция на экране проверки.

Инварианты, которые нельзя потерять:
  * итог проверки несёт id сущности — иначе уборка не по чему целиться;
  * «мёртвые» = только недоступные снаружи (unreachable), а не просто
    ограниченные (restricted) — ограниченная сущность ЖИВА, её не удаляем;
  * удаление строго по владельцу (чужую карточку не тронуть);
  * удаляем из НАШЕЙ базы, а не в Telegram (сущность там уже мертва).
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPW = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "tests"))
from miniapp_source import api_function  # noqa: E402  (ручка в любом модуле API-слоя)
JS = (ROOT / "mini_app" / "screens" / "shield_check.js").read_text(encoding="utf-8")


def _fn_src(src: str, name: str) -> str:
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name} не найдена — ориентир сменился")


def test_check_findings_carry_entity_id():
    src = _fn_src(OPW, "_exec_check_owned_restrictions")
    # находки проблемных сущностей несут id (и bot_id) — иначе уборка нецелевая.
    # ast.unparse нормализует кавычки на одинарные, поэтому сверяем по ним.
    assert "'id': ent['id']" in src and "'bot_id': ent.get('bot_id')" in src, (
        "итог проверки должен нести id сущности, иначе удалять нечем целиться")


def test_dead_list_takes_only_unreachable_not_restricted():
    src = api_function("restrictions_dead")
    assert "'unreachable'" in src or '"unreachable"' in src, (
        "список мёртвых должен брать именно недоступные снаружи")
    # перекрёстная сверка со списком владельца (не предлагаем убрать убранное)
    assert "managed_channels" in src and "managed_bots" in src
    assert "owner_id=$1" in src and "added_by=$1" in src


def test_remove_dead_reuses_scoped_local_disable():
    src = api_function("restrictions_remove_dead")
    assert "owner_id=$1" in src and "cleanup.apply(pool, uid, op_id" in src
    assert "DELETE FROM" not in src
    assert "unreachable" in src


def test_remove_dead_requires_a_selection():
    src = api_function("restrictions_remove_dead")
    assert "не выбрано" in src.lower() or "не выбрана" in src.lower()


def test_shield_links_to_operation_instead_of_direct_mutation():
    assert "restrictions/dead" in JS
    assert "restrictions/remove_dead" not in JS
    assert "d.op_id" in JS and "openOpDetail" in JS

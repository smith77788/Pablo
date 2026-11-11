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

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPW = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
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
    src = _fn_src(API, "restrictions_dead")
    assert "'unreachable'" in src or '"unreachable"' in src, (
        "список мёртвых должен брать именно недоступные снаружи")
    # перекрёстная сверка со списком владельца (не предлагаем убрать убранное)
    assert "managed_channels" in src and "managed_bots" in src
    assert "owner_id=$1" in src and "added_by=$1" in src


def test_remove_dead_is_owner_scoped_and_only_our_base():
    src = _fn_src(API, "restrictions_remove_dead")
    assert "DELETE FROM managed_channels" in src and "owner_id=$1" in src, (
        "удаление каналов обязано скоупиться по владельцу")
    assert "DELETE FROM managed_bots" in src and "added_by=$1" in src, (
        "удаление ботов обязано скоупиться по владельцу")


def test_remove_dead_requires_a_selection():
    src = _fn_src(API, "restrictions_remove_dead")
    assert "не выбрано" in src.lower() or "не выбрана" in src.lower()


def test_screen_confirms_before_removing():
    # Удаление — необратимое действие: экран обязан спросить подтверждение.
    assert "askConfirm(" in JS
    assert "restrictions/remove_dead" in JS and "restrictions/dead" in JS
    # Явно сказано, что это снятие с учёта, а не удаление в Telegram.
    assert "Telegram" in JS

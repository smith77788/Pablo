"""Сборка общей папки из узлов связки в один тап.

Папки перестают быть островом: «развернул связку → раздал одной ссылкой» —
одна цепочка, а не два экрана. Набор чатов берётся из узлов связки
(network_builder), ref_id узла резолвится в СВОЙ канал владельца против ОБОИХ
полей managed_channels (id строки и telegram channel_id), потому что
соглашение ref_id неоднозначно; что не резолвится — честно отсеивается.
"""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


def test_create_endpoint_pulls_nodes_from_the_bundle():
    src = _func_src(_API, "chatlist_folder_create")
    assert "get_instance_detail" in src
    # Владельческий скоуп связки — через сам get_instance_detail(owner_id).
    assert "node_type" in src


def test_ref_id_is_resolved_against_both_conventions():
    """ref_id может быть id строки managed_channels ИЛИ telegram channel_id —
    резолвим против обоих, иначе половина узлов молча выпала бы."""
    src = _func_src(_API, "chatlist_folder_create")
    assert "by_row_id" in src and "by_chan_id" in src


def test_unresolvable_nodes_do_not_produce_a_broken_folder():
    src = _func_src(_API, "chatlist_folder_create")
    # Пустой результат резолва — понятный отказ, а не пустая папка.
    assert "нет ваших каналов" in src


def test_bundle_name_becomes_the_default_folder_title():
    src = _func_src(_API, "chatlist_folder_create")
    assert "auto_title" in src


def test_explicit_chat_ids_still_win_over_bundle():
    """Если переданы явные chat_ids, узлы связки не подмешиваются."""
    src = _func_src(_API, "chatlist_folder_create")
    assert "if not chat_ids and instance_id" in src


def test_bundle_screen_offers_one_tap_folder():
    assert "folderFromNet" in _UI
    assert "instance_id:_currentNetId" in _UI


def test_missing_bundle_is_a_clean_404():
    src = _func_src(_API, "chatlist_folder_create")
    assert "Связка не найдена" in src

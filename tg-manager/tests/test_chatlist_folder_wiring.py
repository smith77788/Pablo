"""Проводка инвайтинга через общие папки: filter_id, операция, API, отказы.

Экспорт chatlist-ссылки — операция (сессия аккаунта, баноопасность, Premium-гейт
Telegram). Сами сетевые типы Telethon здесь не покрыты (заглушка пула их не
связывает — нужна живая проверка), но всё вокруг проверяемо: выбор свободного
id папки, классификация ошибок, регистрация операции, отказ негодного набора до
операции и владельческий скоуп.
"""
from __future__ import annotations

import ast
import pathlib

from services import account_manager as AM

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_OPW = (_ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ── Свободный id папки ──────────────────────────────────────────────────────

def test_free_filter_id_skips_reserved_and_used():
    """0/1 зарезервированы под All/Archive; берём наименьший свободный ≥2."""
    assert AM._pick_free_filter_id([]) == 2
    assert AM._pick_free_filter_id([2, 3, 4]) == 5
    assert AM._pick_free_filter_id([0, 1]) == 2


def test_free_filter_id_fills_a_gap():
    assert AM._pick_free_filter_id([2, 4, 5]) == 3


def test_free_filter_id_ignores_garbage_values():
    assert AM._pick_free_filter_id([None, "x", 2]) == 3


def test_no_free_filter_id_signals_zero():
    assert AM._pick_free_filter_id(list(range(2, 256))) == 0


# ── Операция зарегистрирована и защищена ───────────────────────────────────

def test_folder_export_is_registered_as_an_operation():
    assert '"create_chatlist_folder": _exec_create_chatlist_folder' in _OPW


def test_executor_claims_and_releases_the_account():
    src = _func_src(_OPW, "_exec_create_chatlist_folder")
    assert "try_claim_accounts" in src and "release_accounts" in src
    # Освобождение в finally — иначе аккаунт останется заперт при сбое экспорта.
    assert "finally" in src


def test_executor_explains_the_premium_gate_plainly():
    """Telegram требует Premium для шаринга папок — это не «ошибка», а условие,
    и человек должен понять его без чтения лога."""
    src = _func_src(_OPW, "_exec_create_chatlist_folder")
    assert "premium" in src.lower()
    assert "Premium" in src


def test_executor_persists_result_to_the_draft_row():
    src = _func_src(_OPW, "_exec_create_chatlist_folder")
    assert "set_chatlist_folder_result" in src


# ── Классификация ошибок экспорта ──────────────────────────────────────────

def test_export_error_classifier_is_present():
    src = _func_src(
        (_ROOT / "services" / "account_manager.py").read_text(encoding="utf-8"),
        "create_shared_folder_link")
    for kind in ('"premium"', '"auth"', '"flood"', '"peer"'):
        assert kind in src


# ── API: отказ негодного набора и владельческий скоуп ──────────────────────

def test_create_endpoint_validates_before_enqueuing():
    src = _func_src(_API, "chatlist_folder_create")
    assert "validate_selection" in src
    # Чужие чаты отсекаются по managed_channels владельца.
    assert "managed_channels" in src and "owner_id=$1" in src
    # Экспорт идёт операцией, а не синхронным вызовом Telegram из API.
    assert "create_chatlist_folder" in src and "_obus.submit" in src


def test_delete_is_owner_scoped():
    src = _func_src(_API, "chatlist_folder_delete")
    assert "owner_id=$2" in src


def test_routes_exist():
    assert 'app.router.add_get("/api/miniapp/chatlist_folders"' in _API
    assert 'app.router.add_post("/api/miniapp/chatlist_folders"' in _API
    assert 'app.router.add_delete("/api/miniapp/chatlist_folders/{folder_id}"' in _API


def test_table_is_created_inline_and_by_schema_file():
    assert "CREATE TABLE IF NOT EXISTS chatlist_folders" in _API
    assert (_ROOT / "schema_v199_chatlist_folders.sql").exists()

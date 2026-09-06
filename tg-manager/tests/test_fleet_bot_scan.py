"""Скан флота на ботов: «164 канала/чата, 32 аккаунта и 0 ботов».

Разрыв с живого флота. Скан ресурсов обходит диалоги и собирает Channel, где
аккаунт создатель/админ — каналы и чаты находятся. Но БОТ не Channel: бот,
созданный аккаунтом через @BotFather, в этом обходе не виден никогда. В итоге
раздел «Мои боты» пуст, а единственный способ подключить бота — вставлять токен
руками по одному.

Достоверный источник списка своих ботов — сам @BotFather (/mybots). Разбор его
ответа вынесен в чистую функцию и проверяется здесь; сетевая часть (диалог с
BotFather) на живом Telegram юнит-тестами не покрывается.
"""
from __future__ import annotations

import ast
import pathlib

from services.account_manager import parse_mybots_usernames as P

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_API = (_ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
_OPW = (_ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
_UI = (_ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
_BUS = (_ROOT / "services" / "operation_bus.py").read_text(encoding="utf-8")


def _func_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"функция {name} не найдена")


# ── Разбор ответа BotFather ────────────────────────────────────────────────

def test_extracts_bot_usernames_from_buttons():
    assert P(["@shop_bot", "@news_bot"]) == ["shop_bot", "news_bot"]


def test_navigation_buttons_are_ignored():
    """В клавиатуре BotFather есть «Back»/«Cancel» и пагинация — они не боты."""
    got = P(["@shop_bot", "« Back", "Cancel", "»"])
    assert got == ["shop_bot"]


def test_non_bot_usernames_are_ignored():
    """Правило Telegram: username бота обязан оканчиваться на «bot»."""
    assert P(["@ivan", "@channel_news"]) == []


def test_uppercase_bot_suffix_is_accepted():
    assert P(["@MyShopBot"]) == ["MyShopBot"]


def test_duplicates_collapse_case_insensitively():
    assert P(["@shop_bot", "@Shop_Bot"]) == ["shop_bot"]


def test_label_with_description_takes_the_username_only():
    assert P(["@shop_bot — магазин"]) == ["shop_bot"]


def test_garbage_does_not_crash():
    assert P(None) == []
    assert P(["", None, 123, "@", "@bot"]) == []


def test_usernames_with_invalid_chars_are_rejected():
    assert P(["@bad-name_bot"]) == []


# ── Операция и проводка ────────────────────────────────────────────────────

def test_scan_is_a_registered_operation():
    assert '"scan_owned_bots"' in _BUS, "тип операции обязан быть в OP_REGISTRY"
    assert '"scan_owned_bots": _exec_scan_owned_bots' in _OPW


def test_executor_claims_accounts_and_releases_them():
    src = _func_src(_OPW, "_exec_scan_owned_bots")
    assert "try_claim_accounts" in src and "release_accounts" in src
    assert "finally" in src


def test_executor_paces_botfather_requests():
    """BotFather ловит лимиты при частых обращениях — аккаунты разносим."""
    src = _func_src(_OPW, "_exec_scan_owned_bots")
    assert "asyncio.sleep" in src


def test_found_bots_are_stored_without_a_token():
    """Найденный ≠ подключённый: токена у найденного нет, поэтому отдельная
    таблица, а не managed_bots (token UNIQUE NOT NULL)."""
    src = _func_src(_OPW, "_exec_scan_owned_bots")
    assert "discovered_bots" in src
    assert "ON CONFLICT (owner_id, username)" in src
    # Уже подключённые помечаются, чтобы список не врал.
    assert "linked_bot_id" in src


def test_empty_result_explains_itself():
    """«Ничего не найдено» без объяснения выглядит как поломка скана."""
    src = _func_src(_OPW, "_exec_scan_owned_bots")
    assert "Ботов не найдено" in src


def test_api_endpoints_are_owner_scoped_and_wired():
    scan = _func_src(_API, "bots_scan_fleet")
    assert "_obus.submit" in scan and "scan_owned_bots" in scan
    assert "403" in scan          # отказ по тарифу честным кодом
    lst = _func_src(_API, "bots_discovered_list")
    assert "owner_id=$1" in lst
    assert 'app.router.add_post("/api/miniapp/bots/scan_fleet"' in _API
    assert 'app.router.add_get("/api/miniapp/bots/discovered"' in _API


def test_ui_offers_the_scan_instead_of_only_manual_token():
    assert "scanFleetBots" in _UI and "showDiscoveredBots" in _UI
    # Пустой экран больше не утверждает, что ботов нет.
    assert "Нет подключённых ботов" in _UI


def test_schema_file_exists():
    assert (_ROOT / "schema_v200_discovered_bots.sql").exists()
    assert "CREATE TABLE IF NOT EXISTS discovered_bots" in _API

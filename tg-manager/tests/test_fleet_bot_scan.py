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


# ── Подключение найденных: скан без этого — тупик ──────────────────────────
# Найденный бот виден, но подключить его нечем: токена у него нет. Токен
# отдаёт сам @BotFather по кнопке «API Token».

def test_token_is_parsed_from_botfather_reply():
    from services.account_manager import parse_botfather_token as T

    assert T("Here is the token for @shop_bot:\n123456789:AAH-abcdefghijklmnopqrstuvwxyz012345") \
        == "123456789:AAH-abcdefghijklmnopqrstuvwxyz012345"


def test_token_parser_ignores_text_without_a_token():
    from services.account_manager import parse_botfather_token as T

    assert T("Choose a bot from the list below:") is None
    assert T("") is None
    assert T(None) is None


def test_token_parser_does_not_accept_short_garbage():
    from services.account_manager import parse_botfather_token as T

    assert T("12:short") is None


def test_connect_operation_is_registered():
    assert '"connect_discovered_bots"' in _BUS
    assert '"connect_discovered_bots": _exec_connect_discovered_bots' in _OPW


def test_connect_respects_plan_limit_and_claims_accounts():
    src = _func_src(_OPW, "_exec_connect_discovered_bots")
    assert "get_bot_limit" in src and "get_effective_bot_count" in src, (
        "нельзя подключать больше, чем разрешено тарифом")
    assert "try_claim_accounts" in src and "release_accounts" in src
    assert "finally" in src


def test_connect_only_touches_not_yet_connected():
    src = _func_src(_OPW, "_exec_connect_discovered_bots")
    assert "linked_bot_id IS NULL" in src
    # После подключения отметка проставляется — повтор не берёт того же дважды.
    assert "SET linked_bot_id=$3" in src


def test_connect_never_logs_the_token():
    """Токен — секрет: он не должен попадать ни в лог, ни в текст ошибки."""
    src = _func_src(_OPW, "_exec_connect_discovered_bots")
    assert "log.info(token" not in src and "log.warning(token" not in src
    assert "не удалось подключить" in src, (
        "текст ошибки должен быть без токена")
    fetch = _func_src(
        (_ROOT / "services" / "account_manager.py").read_text(encoding="utf-8"),
        "fetch_bot_tokens_via_botfather")
    assert "НЕ логируем" in fetch


def test_connect_endpoint_wired_and_gated():
    src = _func_src(_API, "bots_connect_discovered")
    assert "connect_discovered_bots" in src and "_obus.submit" in src
    assert "403" in src
    assert "owner_id=$1" in src
    assert 'app.router.add_post("/api/miniapp/bots/connect_discovered"' in _API


def test_ui_offers_bulk_connect():
    assert "connectDiscoveredBots" in _UI
    assert "Подключить найденных" in _UI


# ── Подсказки следующего шага и экран найденных ────────────────────────────
# Новый путь (скан → найденные → подключить) существовал, но продукт про него
# нигде не подсказывал: при «0 ботов» подсказка вела в парсер, мимо того, что
# боты могут уже быть на самих аккаунтах. А «Найденные» были текстом без
# единого действия — подключить конкретного бота было нельзя.

def _suggest(state):
    from services import next_actions as NA

    base = {"acc_active": 5, "bots": 0, "bots_discovered_pending": 0,
            "subscribers": 0, "parsed_recent": 0, "dm_running": 0,
            "funnels": 0, "auto_rules": 0, "channels": 0}
    base.update(state)
    return {s["id"] for s in NA.build_suggestions(base)}


def test_suggests_scanning_the_fleet_when_there_are_no_bots():
    assert "scan_fleet_bots" in _suggest({"bots": 0, "bots_discovered_pending": 0})


def test_suggests_connecting_when_bots_were_found():
    ids = _suggest({"bots": 0, "bots_discovered_pending": 7})
    assert "connect_discovered_bots" in ids
    # Пока есть что подключать, повторно предлагать скан незачем.
    assert "scan_fleet_bots" not in ids


def test_no_scan_suggestion_when_bots_already_connected():
    assert "scan_fleet_bots" not in _suggest({"bots": 3})


def test_connect_suggestion_names_the_number():
    from services import next_actions as NA

    st = {"acc_active": 5, "bots": 0, "bots_discovered_pending": 7,
          "subscribers": 0, "parsed_recent": 0, "dm_running": 0,
          "funnels": 0, "auto_rules": 0, "channels": 0}
    sug = next(s for s in NA.build_suggestions(st)
               if s["id"] == "connect_discovered_bots")
    assert "7" in sug["title"]
    assert sug["fn"] == "connectDiscoveredBots"


def test_state_counts_unconnected_discovered_bots():
    src = (_ROOT / "services" / "next_actions.py").read_text(encoding="utf-8")
    assert "bots_discovered_pending" in src
    assert "linked_bot_id IS NULL" in src


def test_found_bots_is_a_screen_with_actions_not_a_popup():
    assert 'id="s-foundbots"' in _UI
    assert "connectOneBot" in _UI, "должно быть подключение конкретного бота"
    assert "openBotByUsername" in _UI, "у подключённого — вход в меню управления"


def test_schema_file_exists():
    assert (_ROOT / "schema_v200_discovered_bots.sql").exists()
    assert "CREATE TABLE IF NOT EXISTS discovered_bots" in _API

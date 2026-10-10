"""Массовое включение bot-to-bot: чистый матчер меню + проводка операции.

Разбор ответов @BotFather на живом Telegram непокрываем (заглушка типы не
связывает), поэтому вся логика вынесена в чистые функции: найти кнопку
bot-to-bot и определить её состояние, сгруппировать ботов по аккаунту-владельцу.
Их и проверяем; сетевой сив только помечен подключённым.
"""
from __future__ import annotations

import ast
import pathlib

from services import bot_b2b as B

ROOT = pathlib.Path(__file__).resolve().parents[1]
OPW = (ROOT / "services" / "op_worker.py").read_text(encoding="utf-8")
API = (ROOT / "services" / "mini_app_api.py").read_text(encoding="utf-8")
UI = (ROOT / "mini_app" / "index.html").read_text(encoding="utf-8")
BUS = (ROOT / "services" / "operation_bus.py").read_text(encoding="utf-8")


# ── Матчер кнопки bot-to-bot ───────────────────────────────────────────────

def test_finds_toggle_and_reads_off_state():
    r = B.find_b2b_toggle(["Bot Settings", "Turn on Bot-to-Bot Mode", "Back"])
    assert r["button"] == "Turn on Bot-to-Bot Mode" and r["state"] == "off"


def test_reads_on_state_from_turn_off():
    r = B.find_b2b_toggle(["Turn off Bot-to-Bot Mode"])
    assert r["state"] == "on"


def test_reads_on_state_from_checkmark():
    r = B.find_b2b_toggle(["✅ Bot-to-Bot"])
    assert r["state"] == "on"


def test_russian_labels_are_matched():
    r = B.find_b2b_toggle(["Включить бот-бот режим"])
    assert r["button"] and r["state"] == "off"


def test_absent_toggle_is_reported_not_faked():
    r = B.find_b2b_toggle(["Payments", "Edit Botpic", "Delete Bot"])
    assert r["button"] is None and r["state"] == "unknown"


def test_ambiguous_state_is_unknown_not_guessed():
    r = B.find_b2b_toggle(["Bot-to-Bot"])
    assert r["button"] == "Bot-to-Bot" and r["state"] == "unknown"


# ── Группировка по аккаунту-владельцу ──────────────────────────────────────

def test_groups_bots_by_owner_account():
    bots = [
        {"username": "a", "acc_id": 1, "b2b_enabled": False},
        {"username": "b", "acc_id": 1, "b2b_enabled": False},
        {"username": "c", "acc_id": 2, "b2b_enabled": False},
    ]
    plan = B.plan_targets(bots)
    assert set(plan[1]) == {"a", "b"} and plan[2] == ["c"]


def test_already_enabled_are_skipped_by_default():
    bots = [{"username": "a", "acc_id": 1, "b2b_enabled": True},
            {"username": "b", "acc_id": 1, "b2b_enabled": False}]
    assert B.plan_targets(bots) == {1: ["b"]}


def test_only_disabled_false_includes_all():
    bots = [{"username": "a", "acc_id": 1, "b2b_enabled": True}]
    assert B.plan_targets(bots, only_disabled=False) == {1: ["a"]}


def test_bots_without_owner_account_group_under_none():
    bots = [{"username": "a", "acc_id": None, "b2b_enabled": False}]
    assert None in B.plan_targets(bots)


def test_bot_without_username_is_dropped():
    bots = [{"username": "", "acc_id": 1, "b2b_enabled": False}]
    assert B.plan_targets(bots) == {}


def test_plan_respects_limit():
    bots = [{"username": f"b{i}", "acc_id": 1, "b2b_enabled": False} for i in range(10)]
    total = sum(len(v) for v in B.plan_targets(bots, limit=3).values())
    assert total == 3


# ── Проводка операции ──────────────────────────────────────────────────────

def _fn(src, name):
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    raise AssertionError(f"{name} не найдена")


def test_op_registered_and_dispatched():
    assert '"enable_bot_to_bot"' in BUS
    assert '"enable_bot_to_bot": _exec_enable_bot_to_bot' in OPW


def test_executor_groups_and_marks_enabled():
    body = _fn(OPW, "_exec_enable_bot_to_bot")
    assert "plan_targets" in body
    assert "b2b_enabled=TRUE" in body
    assert "release_accounts" in body       # аккаунт-владельца захватываем и отпускаем


def test_executor_reports_bots_without_owner_honestly():
    body = _fn(OPW, "_exec_enable_bot_to_bot")
    assert "no_owner" in body and "вручную" in body


def test_endpoint_and_route_exist():
    assert "async def bots_enable_b2b" in API
    assert 'add_post("/api/miniapp/bots/enable_b2b"' in API


def test_ui_starts_with_a_canary():
    assert "enableBotMesh" in UI
    assert "limit: 2" in UI          # первый прогон — канарейка на 2 ботах


def test_schema_mirrored():
    assert "b2b_enabled BOOLEAN" in API
    assert (ROOT / "schema_v213_bot_b2b.sql").exists()

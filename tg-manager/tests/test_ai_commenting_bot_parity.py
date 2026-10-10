"""Паритет: AI-комментирование из бота (ai_comment_submit).

Раньше AI Commenting запускался только из mini-app. Бот получил тот же поток
(каналы → ниша → тон) поверх op 'ai_comment' через operation_bus. Исполнитель
и движок уже существуют.
"""
from __future__ import annotations

import os

import tests.conftest  # noqa: F401 — стабы telethon/aiogram

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_handler_imports_and_router():
    import bot.handlers.ai_commenting as ai
    assert ai.router is not None
    for fn in ("cmd_ai_comment", "cb_ai_comment_open", "cb_ai_comment_cancel",
               "msg_ai_comment_channels", "msg_ai_comment_niche", "cb_ai_comment_tone"):
        assert hasattr(ai, fn), f"нет хендлера {fn}"


def test_submits_ai_comment_op_via_bus():
    h = _read("bot/handlers/ai_commenting.py")
    assert "operation_bus.submit(" in h
    assert '"ai_comment"' in h
    # передаёт те же ключи параметров, что и mini-app
    for key in ('"channels"', '"niche"', '"tone"', '"acc_count"'):
        assert key in h, f"нет параметра {key}"


def test_applies_content_safety_gate():
    h = _read("bot/handlers/ai_commenting.py")
    assert "content_safety.enforce(" in h
    assert 'surface="ai_comment"' in h
    assert "verdict.blocked" in h


def test_op_registered_and_executor_exists():
    assert '"ai_comment":' in _read("services/operation_bus.py")
    ow = _read("services/op_worker.py")
    assert "async def _exec_ai_comment(" in ow


def test_router_registered_in_main():
    m = _read("main.py")
    assert "ai_commenting as ai_commenting_handler" in m
    assert "dp.include_router(ai_commenting_handler.router)" in m


def test_command_present():
    assert 'Command("ai_comment")' in _read("bot/handlers/ai_commenting.py")

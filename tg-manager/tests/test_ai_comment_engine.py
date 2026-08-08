"""Регрессия: AI Commenting — чистые хелперы + проводка op/эндпоинт.

Сетевой постинг и вызов LLM тестируются интеграционно; здесь — построение промпта,
санитайзинг ответа LLM и guard'ы, что op_type ai_comment диспетчеризован, эндпоинт
и UI подключены (иначе — мёртвая фича).
"""
from __future__ import annotations

import os

from services.ai_comment_engine import (
    build_comment_prompt, sanitize_comment, COMMENT_TONES,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_prompt_includes_post_niche_and_tone():
    system, user = build_comment_prompt("Запустили фичу X", niche="стартапы", tone="expert")
    assert "стартапы" in system
    assert COMMENT_TONES["expert"] in system
    assert "Запустили фичу X" in user
    # запрет ссылок/хэштегов/раскрытия бота — обязателен
    assert "без ссылок" in system.lower() and "хэштег" in system.lower()


def test_prompt_unknown_tone_falls_back_neutral():
    system, _ = build_comment_prompt("пост", tone="bogus")
    assert COMMENT_TONES["neutral"] in system


def test_prompt_truncates_long_post():
    long_post = "a" * 5000
    _, user = build_comment_prompt(long_post)
    # пост обрезается (не тащим 5000 символов в промпт)
    assert user.count("a") <= 1500


def test_sanitize_strips_quotes_prefix_and_caps_length():
    assert sanitize_comment('  "Отличный пост"  ') == "Отличный пост"
    assert sanitize_comment("«круто»") == "круто"
    assert sanitize_comment("Комментарий: супер") == "супер"
    assert sanitize_comment("ответ — норм") == "норм"
    assert len(sanitize_comment("x" * 999)) == 280
    assert sanitize_comment("") == "" and sanitize_comment(None) == ""


def test_op_and_endpoint_and_ui_wired():
    ow = _read("services/op_worker.py")
    assert 'op_type == "ai_comment"' in ow and "_exec_ai_comment(" in ow
    # content-safety guard присутствует в исполнителе
    seg = ow[ow.index("async def _exec_ai_comment"):ow.index("async def _exec_niche_growth_post")]
    assert "content_safety.enforce" in seg
    api = _read("services/mini_app_api.py")
    assert "async def ai_comment_submit" in api
    assert "'ai_comment','pending'" in api
    assert 'add_post("/api/miniapp/ai_comment", ai_comment_submit)' in api
    ui = _read("mini_app/index.html")
    assert "submitAiComment" in ui and "/api/miniapp/ai_comment" in ui

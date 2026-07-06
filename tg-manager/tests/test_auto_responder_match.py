"""Регрессия: мультиключевой авто-ответ должен работать во всех match_mode.

До фикса разбиение keyword по запятой применялось только в contains — правила
с match_mode=exact/starts и несколькими ключами через запятую никогда не
срабатывали (сравнение шло с целой строкой "ключ1, ключ2"), без единой ошибки.
"""
from __future__ import annotations

from services.auto_responder import _match_rule


def _rule(keyword: str, match_mode: str) -> dict:
    return {"trigger_type": "keyword", "keyword": keyword, "match_mode": match_mode}


def test_exact_mode_multi_keyword():
    rule = _rule("цена, стоимость", "exact")
    assert _match_rule(rule, "цена") is True
    assert _match_rule(rule, "стоимость") is True
    assert _match_rule(rule, "цена вопроса") is False


def test_starts_mode_multi_keyword():
    rule = _rule("привет, здравствуй", "starts")
    assert _match_rule(rule, "приветствую всех") is True
    assert _match_rule(rule, "здравствуйте!") is True
    assert _match_rule(rule, "добрый день") is False


def test_contains_mode_single_keyword_unaffected():
    rule = _rule("скидка", "contains")
    assert _match_rule(rule, "есть скидка?") is True
    assert _match_rule(rule, "нет") is False


def test_contains_mode_multi_keyword():
    rule = _rule("акция, промокод", "contains")
    assert _match_rule(rule, "у вас есть промокод?") is True

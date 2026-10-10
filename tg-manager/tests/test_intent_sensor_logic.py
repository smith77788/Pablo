"""Сенсор намерений: два дефекта в самой логике распознавания и движения стадии.

1. Подстрочное совпадение срабатывало на ОТРИЦАНИИ. Фраза «готов» входит в
   рекомендованный набор, поэтому «не готов» трактовалось как готовность купить
   и двигало клиента в «Переговоры» — ровно наоборот сказанному.

2. Стадия менялась на любую совпавшую, БЕЗ правил движения: клиент из
   «Выиграно», спросивший «а какая цена на второй заказ», откатывался в
   «Предложение». Плюс общий ранг ставил lost (5) выше won (4) просто по
   позиции в кортеже — фраза-отказ перебивала закрытую сделку.
"""
from __future__ import annotations

import pytest

from services.intent_sensor import _phrase_matches, decide_stage


# ── Отрицание ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["не готов", "я не готов пока", "пока не готов"])
def test_negation_blocks_match(text):
    assert not _phrase_matches("готов", text)


def test_plain_match_still_works():
    assert _phrase_matches("готов", "готов купить")
    assert _phrase_matches("цена", "какая цена?")


def test_later_positive_occurrence_still_matches():
    """Отрицали одно вхождение — не повод игнорировать следующее."""
    assert _phrase_matches("готов", "не готов сегодня, но завтра готов")


def test_rule_that_is_itself_a_negation_keeps_working():
    """«не интересно» — правило про отказ, отрицание в нём и есть смысл."""
    assert _phrase_matches("не интересно", "не интересно")
    assert _phrase_matches("не интересно", "мне не интересно")


def test_word_before_is_compared_whole():
    """«мне» не должно считаться частицей «не» — иначе верное совпадение глохнет."""
    assert _phrase_matches("интересно", "мне интересно")


@pytest.mark.parametrize("neg", ["нет", "без", "никогда"])
def test_other_negations(neg):
    assert not _phrase_matches("оплата", f"{neg} оплата")


def test_alternatives_respect_negation():
    assert _phrase_matches("цена|прайс", "пришлите прайс")
    assert not _phrase_matches("цена|прайс", "без прайса")


def test_regexp_rules_are_not_touched_by_negation_guard():
    """В регэкспе автор выражает условие сам — вмешиваться нельзя."""
    assert _phrase_matches(r"re:\d{4,}\s*руб", "не 5000 руб")


def test_broken_regexp_never_crashes():
    assert not _phrase_matches("re:[unclosed", "любой текст")


def test_empty_inputs():
    assert not _phrase_matches("", "текст")
    assert not _phrase_matches("цена", "")


# ── Движение стадии ───────────────────────────────────────────────────────────

def test_moves_forward():
    assert decide_stage("lead", ["proposal"]) == "proposal"
    assert decide_stage(None, ["proposal"]) == "proposal"


def test_never_moves_backward():
    """Главный дефект: закрытая сделка откатывалась вопросом про цену."""
    assert decide_stage("won", ["proposal"]) is None
    assert decide_stage("negotiation", ["proposal"]) is None


def test_lost_does_not_cancel_a_won_deal():
    assert decide_stage("won", ["lost"]) is None


def test_lost_applies_from_active_stages():
    assert decide_stage("negotiation", ["lost"]) == "lost"
    assert decide_stage(None, ["lost"]) == "lost"


def test_explicit_refusal_outweighs_positive_match_in_same_message():
    assert decide_stage("lead", ["negotiation", "lost"]) == "lost"


def test_client_can_return_from_lost():
    """Передумал — возвращаем в воронку."""
    assert decide_stage("lost", ["proposal"]) == "proposal"


def test_no_change_returns_none():
    assert decide_stage("proposal", ["proposal"]) is None
    assert decide_stage("lost", ["lost"]) is None


def test_picks_the_most_advanced_positive_stage():
    assert decide_stage("lead", ["proposal", "negotiation"]) == "negotiation"


def test_unknown_or_empty_stages_are_ignored():
    assert decide_stage("lead", []) is None
    assert decide_stage("lead", ["не-стадия"]) is None


def test_scan_uses_the_decision_function():
    import ast
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "services" / "intent_sensor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "scan_incoming":
            body = ast.get_source_segment(src, node)
            assert "decide_stage(" in body, "сканер обязан ходить через правила движения"
            assert "max(stages, key=" not in body, "старый выбор «по рангу» вернулся"
            return
    raise AssertionError("scan_incoming не найдена")

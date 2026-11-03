"""Условие сценария сравнивает поле не только на «равно».

ЧТО БЫЛО. Условие умело «поле равно» и «поле заполнено». Ветку «бюджет больше
50 000 — менеджеру, меньше — в автоворонку» или «в городе есть "Москва"»
собрать было нельзя: любое число сравнивалось как текст.
"""
import pytest

from services import workflow_engine as wf
from services.workflow_runtime import compare_field


def test_numbers_are_compared_as_numbers():
    assert compare_field("field_gt", "1 500", "999")
    assert compare_field("field_lt", "99,5", "100")
    assert not compare_field("field_gt", "", "0")
    assert not compare_field("field_gt", "много", "10")


def test_text_contains_and_not_equals():
    assert compare_field("field_contains", "г. Москва, Арбат", "москва")
    assert not compare_field("field_contains", "Казань", "москва")
    assert compare_field("field_not_equals", "нет", "да")
    assert compare_field("field_equals", "Ёлка", "елка")


def test_the_step_is_checked_on_save():
    step = wf.validate_step({"type": "condition", "condition": "field_gt",
                             "condition_value": "Budget = 50000"})
    assert step["condition_value"] == "budget=50000"
    with pytest.raises(ValueError):
        wf.validate_step({"type": "condition", "condition": "field_gt",
                          "condition_value": "budget=много"})
    with pytest.raises(ValueError):
        wf.validate_step({"type": "condition", "condition": "field_contains",
                          "condition_value": "city"})

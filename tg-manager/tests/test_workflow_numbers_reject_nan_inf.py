"""Сценарии: «nan» и «inf» не числа.

float() принимает «nan», «inf», «1e309». NaN проходил потолок шага
«поле+=число» (`abs(nan) > 1e6` ложно): шаг «score+=nan» сохранялся и
навсегда превращал баллы лида в «nan». Ответ подписчика «inf» в поле ломал
сравнения «больше/меньше». Падает без фикса.
"""
from __future__ import annotations

import pytest

from services import workflow_engine as wf
from services import workflow_runtime as rt


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "1e309", "Infinity"])
def test_non_finite_is_not_a_number(raw):
    assert wf._to_number(raw) is None


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e309"])
def test_add_to_field_step_refuses_non_finite(raw):
    with pytest.raises(ValueError):
        wf.validate_step({"type": "action", "action": "add_to_field",
                          "value": f"score+={raw}"})


def test_poisoned_field_restarts_from_zero():
    assert rt.add_to_number("inf", "5") == "5"
    assert rt.add_to_number("nan", "2") == "2"
    assert wf._to_number("1 500,5") == 1500.5

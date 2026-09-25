"""Отказ по тарифу приходил по-английски и не вёл к оплате.

`operation_bus.submit` поднимает `PlanRequiredError`, и сорок с лишним
хендлеров мини-аппа отдают её текст пользователю как `_err(str(exc), 403)`.
Текст был `operation 'mass_invite' requires plan 'paid'` — то есть владелец,
который по-английски не читает, в ответ на нажатие «Запустить» получал
непонятную техническую строку.

Хуже того, единый маркер пейволла в `_err` ставится по слову «подписка»: без
него отказ показывался сухим тостом вместо экрана оформления. В лучшей точке
конверсии — человек только что упёрся в платную возможность — путь к оплате
обрывался. Оба теста-сторожа при этом были зелёными: `test_plan_gate_surfaced`
проверяет, что отказ доходит как 403 с причиной, `test_paywall_upgrade_sheet` —
что механизм маркировки стоит в одном месте. Никто не проверял сам ТЕКСТ.

Заодно сплошной проверкой закрыт весь класс: у 403-отказа мини-аппа не может
быть английского текста («Forbidden», «forbidden»).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from services import operation_bus

API = Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"
CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def test_plan_refusal_is_russian():
    msg = str(operation_bus.PlanRequiredError("mass_invite", "paid"))
    assert CYRILLIC.search(msg), f"владелец не читает по-английски: {msg}"
    assert "requires plan" not in msg


def test_plan_refusal_names_the_operation():
    """«Что-то платное» бесполезно: человек должен понять, за что платит."""
    msg = str(operation_bus.PlanRequiredError("mass_publish", "paid"))
    assert operation_bus.OP_REGISTRY["mass_publish"]["description"] in msg


def test_unknown_op_still_gives_a_usable_refusal():
    msg = str(operation_bus.PlanRequiredError("небывалый_тип", "paid"))
    assert CYRILLIC.search(msg) and "подписк" in msg.lower()


def test_plan_refusal_triggers_the_upgrade_sheet():
    """Маркер пейволла в `_err` ставится по слову «подписка» — оно обязано быть."""
    msg = str(operation_bus.PlanRequiredError("mass_invite", "paid"))
    assert "подписк" in msg.lower(), (
        "без этого слова отказ покажется сухим тостом, и путь к оплате оборвётся")


def test_technical_detail_stays_available_for_logs():
    exc = operation_bus.PlanRequiredError("mass_invite", "paid")
    assert exc.op_type == "mass_invite" and exc.required_plan == "paid"


# ── Сплошная проверка: у 403 мини-аппа нет английского текста ────────────────

def _err_403_literals() -> list[tuple[int, str]]:
    tree = ast.parse(API.read_text("utf-8"))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_err"):
            continue
        if len(node.args) < 2:
            continue
        status = node.args[1]
        if not (isinstance(status, ast.Constant) and status.value == 403):
            continue
        msg = node.args[0]
        if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
            out.append((node.lineno, msg.value))
    return out


def test_detector_sees_the_refusals():
    """Страховка измерителя: пустой список превратил бы проверку в заглушку."""
    assert len(_err_403_literals()) > 20


def test_no_english_403_in_the_mini_app():
    bad = [(ln, m) for ln, m in _err_403_literals() if not CYRILLIC.search(m)]
    assert not bad, (
        "отказ уйдёт пользователю по-английски:\n  "
        + "\n  ".join(f"services/mini_app_api.py:{ln}  {m!r}" for ln, m in bad))

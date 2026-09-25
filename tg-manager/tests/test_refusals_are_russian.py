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

Заодно сплошной проверкой закрыт весь класс. Английскими были не только
отказы по тарифу: из 1151 английского `_err` пользователь ВИДИТ 516 — это
статусы 400, 403 и 404, которые мини-апп показывает как есть («Not found»,
«Invalid JSON», «bad bot_id»). Статусы 401 и 500 в проверку не входят
намеренно: их мини-апп заменяет своими словами, и английское тело туда не
доходит.
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


# ── Сплошная проверка: отказ мини-аппа не может быть на английском ──────────

# Статусы, чей текст пользователь ВИДИТ. 401 и 500 сюда не входят намеренно:
# мини-апп заменяет их своими словами (на 401 молча обновляет токен, на 500
# показывает «Внутренняя ошибка сервиса»), поэтому английское тело туда не
# доходит. 403, 400 и 404 показываются как есть.
USER_VISIBLE_STATUSES = (400, 403, 404)


def _err_literals() -> list[tuple[int, int, str]]:
    """(строка, статус, текст) для каждого `_err` с литеральным сообщением."""
    tree = ast.parse(API.read_text("utf-8"))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_err"):
            continue
        if not node.args:
            continue
        status = 400          # умолчание самого _err
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            status = node.args[1].value
        msg = node.args[0]
        if isinstance(msg, ast.Constant) and isinstance(msg.value, str):
            out.append((node.lineno, status, msg.value))
    return out


def test_detector_sees_the_refusals():
    """Страховка измерителя: пустой список превратил бы проверку в заглушку."""
    visible = [x for x in _err_literals() if x[1] in USER_VISIBLE_STATUSES]
    assert len(visible) > 400, f"измеритель нашёл всего {len(visible)} отказов"


def test_no_english_refusal_reaches_the_user():
    bad = [(ln, st, m) for ln, st, m in _err_literals()
           if st in USER_VISIBLE_STATUSES and not CYRILLIC.search(m)]
    assert not bad, (
        f"отказ уйдёт пользователю по-английски ({len(bad)} шт.), "
        "а владелец по-английски не читает:\n  "
        + "\n  ".join(f"services/mini_app_api.py:{ln} [{st}] {m!r}"
                      for ln, st, m in sorted(bad)[:25]))

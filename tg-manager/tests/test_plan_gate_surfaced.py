"""Отказ по тарифу обязан доходить до пользователя как 403 с причиной.

ЧТО БЫЛО СЛОМАНО. `operation_bus.submit` централизованно проверяет `min_plan` и
поднимает `PlanRequiredError` (наследник `PermissionError`). Но из 42 хендлеров
мини-аппа, которые ставят операции, перехват был ровно у 9 — остальные ловили это
общим `except Exception` и отдавали `_err(str(exc), 500)`.

Как это выглядело для пользователя без подписки: он нажимает «Запустить», и
вместо «операция платная» получает внутреннюю ошибку сервера с текстом
`operation 'mass_invite' requires plan 'pro'`. То есть платная функция выглядела
СЛОМАННОЙ, а не платной — путь к покупке обрывался на пустом месте, и поддержка
получала жалобу на баг вместо вопроса о тарифе.

Один хендлер (`seo_apply_all`) не имел вообще никакого try/except вокруг submit —
там отказ улетал бы наружу HTML-страницей aiohttp, даже не JSON.
"""
from __future__ import annotations

import re
from pathlib import Path

API = Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"


def _handlers() -> list[tuple[str, str, int]]:
    """(имя, тело, номер строки) для каждого async-хендлера мини-аппа."""
    lines = API.read_text(encoding="utf-8").split("\n")
    marks = []
    for i, l in enumerate(lines):
        m = re.match(r"    async def ([a-z_0-9]+)\(request", l)
        if m:
            marks.append((i, m.group(1)))
    marks.append((len(lines), "__end__"))
    return [(name, "\n".join(lines[s:e]), s + 1)
            for (s, name), (e, _) in zip(marks, marks[1:])]


def test_every_submitting_handler_surfaces_plan_refusal():
    """Гейт-храповик: новый хендлер с submit() обязан отдавать 403, а не 500."""
    bad = [(n, ln) for n, body, ln in _handlers()
           if "submit(" in body and not re.search(r"except\s+.*PermissionError", body)]
    assert not bad, (
        "отказ по тарифу уйдёт сырым 500 — платная функция выглядит сломанной:\n"
        + "\n".join(f"  services/mini_app_api.py:{ln}  {n}" for n, ln in bad)
    )


def test_refusal_is_403_with_reason():
    """403 без причины бесполезен: пользователь должен понять, что это тариф."""
    for name, body, ln in _handlers():
        if "submit(" not in body:
            continue
        m = re.search(r"except\s+.*PermissionError as exc:(.{0,400})", body, re.DOTALL)
        assert m, name
        blk = m.group(1)
        assert "403" in blk, f"{name}: отказ по тарифу — не ошибка сервера"
        assert "exc" in blk or "подписк" in blk.lower(), (
            f"{name}: причина отказа должна доходить до пользователя"
        )


def test_middleware_catches_unguarded_handlers():
    """Страховка для хендлеров, которые добавят без перехвата: PermissionError,
    вылетевший наружу, всё равно становится 403, а не 500 от aiohttp."""
    src = API.read_text(encoding="utf-8")
    assert "plan_gate_middleware" in src, "нужна страховка на уровне приложения"
    m = re.search(r"async def plan_gate_middleware.*?app\.middlewares\.append\(plan_gate_middleware\)",
                  src, re.DOTALL)
    assert m, "middleware обязан быть зарегистрирован, иначе он мёртвый код"
    assert "except PermissionError" in m.group(0) and "403" in m.group(0)


def test_plan_error_is_a_permission_error():
    """Перехват держится на этом наследовании — если оно уйдёт, все 42 хендлера
    молча вернутся к 500."""
    from services import operation_bus
    assert issubclass(operation_bus.PlanRequiredError, PermissionError)


def test_seo_apply_all_no_longer_bare():
    """Единственный submit вообще без try/except: отказ улетал бы HTML-страницей."""
    body = next(b for n, b, _ in _handlers() if n == "seo_apply_all")
    assert "except PermissionError" in body

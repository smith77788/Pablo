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

import ast
import re
from pathlib import Path

API = Path(__file__).resolve().parents[1] / "services" / "mini_app_api.py"


def _handlers() -> list[tuple[str, str, int]]:
    """(имя, тело, номер строки) для каждого async-обработчика мини-аппа.

    Границы берём из AST. Раньше тело резалось по тексту — от одной строки
    `    async def имя(request` до следующей такой же, — и любая async-функция
    с первым аргументом `request`, которая НЕ хендлер, склеивала в своё «тело»
    весь код до следующего хендлера. Так и вышло: внешняя middleware с потолком
    времени запроса утянула в себя чужой `submit(`, и храповик показал отказ по
    тарифу там, где его нет. Детектор, дающий находку в здоровом коде, сломан
    сам — правило из CLAUDE.md.

    Текстовая починка границ (резать до первой строки уровня модуля) ту
    находку тоже убирала, но обработчики искала по отступу ровно в четыре
    пробела — вложенные глубже в детектор не попадали вовсе. AST видит все.
    """
    src = API.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Границы — по номерам строк узла. `ast.get_source_segment` здесь нельзя:
    # он режет весь исходник на строки на КАЖДЫЙ вызов, а файл больше мегабайта
    # и обработчиков почти семь сотен — один прогон храповика занимал 80 секунд.
    lines = src.splitlines(keepends=True)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        args = node.args.args
        if not args or args[0].arg != "request":
            continue
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        body = "".join(lines[start:end])
        out.append((node.name, body, node.lineno))
    return out


def test_the_detector_sees_the_handlers_and_their_real_bodies():
    """Самопроверка измерителя до того, как верить его пустому списку."""
    found = _handlers()
    assert len(found) > 30, f"хендлеры мини-аппа потерялись: {len(found)}"
    names = {n for n, _, _ in found}
    assert "retry_operation" in names and "cancel_operation" in names
    # тело каждого обработчика — только его собственный код
    for name, body, _ln in found:
        assert body.lstrip().startswith("async def "), name
        assert body.count("\nasync def ") == 0, (
            f"{name}: в тело попал код соседней функции — находки будут ложными")


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

"""Глобальный обработчик ошибок не должен падать на несуществующем методе Enum.

Баг: main._global_error_handler звал ErrorCode.from_exception(exc). Но ErrorCode —
это Enum, а from_exception — МОДУЛЬНАЯ функция services.error_codes (её так и зовут
error_reporting/error_monitor). ErrorCode.from_exception → AttributeError на ВТОРОЙ
строке глобального обработчика: пользователь не получал ⚠️-ответа, report_error не
вызывался, а сам обработчик валился вторичной ошибкой. Последняя линия защиты была
сломана.

Тест лёгкий (тянет только error_codes, без aiogram/telethon/БД), поэтому идёт в CI.
"""
from __future__ import annotations

import os

from services.error_codes import ErrorCode, from_exception


def test_from_exception_is_module_function_returning_errorcode():
    # from_exception — вызываемая модуль-функция и возвращает член ErrorCode
    assert callable(from_exception)
    assert isinstance(from_exception(ValueError("x")), ErrorCode)
    assert isinstance(from_exception(RuntimeError("y")), ErrorCode)


def test_errorcode_enum_has_no_from_exception_attribute():
    # Именно поэтому ErrorCode.from_exception(...) — баг: у Enum такого атрибута нет.
    assert not hasattr(ErrorCode, "from_exception"), (
        "если у ErrorCode появился from_exception — обнови тест; сейчас его нет, "
        "и вызов ErrorCode.from_exception(exc) падает AttributeError"
    )


def test_global_error_handler_uses_module_from_exception():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    # main импортирует модульную from_exception
    assert "from services.error_codes import" in src and "from_exception" in src
    # и НЕ зовёт несуществующий метод Enum
    assert "ErrorCode.from_exception(" not in src, (
        "ErrorCode.from_exception(exc) → AttributeError в глобальном обработчике; "
        "нужно звать модульную from_exception(exc)"
    )
    # обработчик реально использует результат
    assert "error_code = from_exception(exc)" in src

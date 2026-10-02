"""Стабильность: пользователь никогда не видит сырой краш вместо ответа.

1) Глобальная страховка API-middleware: любое необработанное исключение
   хендлера → чистый JSON-500 (а не HTML-краш, который мини-апп не распарсит);
   штатные HTTP-ответы (404/редиректы) пропускаются.
2) Обязательные переменные окружения падают с ВНЯТНЫМ сообщением, а не с
   крипто-KeyError, роняющим всё приложение.
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_api_middleware_catches_all_unhandled():
    src = open(os.path.join(ROOT, "services", "mini_app_api.py"), encoding="utf-8").read()
    i = src.index("async def plan_gate_middleware")
    body = src[i:i + 1400]
    assert "except web.HTTPException:" in body and "raise" in body   # штатные ответы не глушим
    assert "except Exception" in body                                # ловим ВСЁ остальное
    # Текст внутренней ошибки живёт в одной константе `_INTERNAL_ERROR`
    # (раньше здесь была дословная строка в каждом из 398 обработчиков).
    assert "return _err(_INTERNAL_ERROR" in body                     # чистый JSON-500
    from services.mini_app_api import _INTERNAL_ERROR
    assert "Внутренняя ошибка сервера" in _INTERNAL_ERROR, (
        "ответ на 500 обязан быть по-русски: владелец английского не читает")
    assert "log.exception" in body                                   # с логом для диагностики


def test_config_require_clear_error():
    import config
    # присутствующая переменная возвращается
    assert config._require("PATH")
    # отсутствующая → RuntimeError с внятным текстом, а не KeyError
    with pytest.raises(RuntimeError) as ei:
        config._require("INFRAGRAM_DEFINITELY_MISSING_VAR_XYZ")
    assert "переменная окружения" in str(ei.value)

"""Сквозная защита от ошибок: пользователь не видит сырых 500 и не застревает.

- api() для статуса 500 показывает generic-сообщение, а не сырой str(exc) (нечитаемо/
  утечка); деталь остаётся в серверных логах.
- глобальный unhandledrejection-хендлер превращает любую непойманную ошибку в тост,
  а не в молчаливый фриз экрана → сценарий всегда доходит до видимого результата.
"""
from __future__ import annotations
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parent.parent / "mini_app" / "index.html").read_text(encoding="utf-8")


def test_api_sanitizes_500():
    """Разбор ответа живёт в `_apiRaw`: `api()` стала тонкой обёрткой над ним,
    когда в неё добавили гашение двойного тапа. Проверка смотрит туда, где
    разбор, и заодно требует, чтобы `api()` по-прежнему вела в ту же функцию, —
    иначе санитайзер остался бы на месте, но в обход."""
    m = re.search(r"async function _apiRaw\(path, opts=\{\}\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert m, "_apiRaw() не найден"
    body = m.group(1)
    assert "r.status === 500" in body and "Внутренняя ошибка сервиса" in body, \
        "500 должен показывать generic, а не сырой exc"
    outer = re.search(r"async function api\(path, opts=\{\}\)\s*\{(.*?)\n\}", HTML, re.DOTALL)
    assert outer, "api() не найден"
    assert "_apiRaw(" in outer.group(1), "api() шлёт запрос мимо разбора ошибок"


def test_global_unhandledrejection_handler():
    assert "addEventListener('unhandledrejection'" in HTML, "нет глобальной сети безопасности"
    # показывает тост (не молчит)
    i = HTML.index("addEventListener('unhandledrejection'")
    seg = HTML[i:i+400]
    assert "toast(" in seg


def test_global_error_logger():
    assert "addEventListener('error'" in HTML

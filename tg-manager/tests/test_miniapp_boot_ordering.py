"""boot() не должен обращаться к const/let, объявленным ПОСЛЕ него (TDZ).

ЧТО БЫЛО. Реструктуризация верхней части скрипта оставила инициализирующий IIFE
`boot()` ВЫШЕ объявления `const FETCH_TIMEOUT_MS`. boot() СИНХРОННО (первой же
строкой) вызывает `fetchT('/api/miniapp/auth')`, а дефолт fetchT —
`ms = FETCH_TIMEOUT_MS`. Пока строка `const FETCH_TIMEOUT_MS = 30000` не
выполнилась, константа в «мёртвой зоне» (TDZ) → бросается
«Cannot access 'FETCH_TIMEOUT_MS' before initialization». Ошибку глотал
собственный catch внутри boot(), поэтому скрипт «не умирал», но авторизации не
было и данные не грузились — приложение показывало только статичные плитки и
красную плашку с этой ошибкой (см. скрин пользователя).

Почему прошлый тест «скрипт не умирает без SDK» это НЕ поймал: boot() ловит
исключение сам, и `new Function(src)()` завершается штатно. Нужен именно контроль
ПОРЯДКА объявлений.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "mini_app", "index.html")


def _html() -> str:
    with open(INDEX, encoding="utf-8") as f:
        return f.read()


def test_fetch_timeout_declared_before_boot():
    html = _html()
    i_const = html.find("const FETCH_TIMEOUT_MS")
    i_boot = html.find("function boot()")
    assert i_const != -1, "не найдено объявление FETCH_TIMEOUT_MS"
    assert i_boot != -1, "не найден инициализатор boot()"
    assert i_const < i_boot, (
        "const FETCH_TIMEOUT_MS объявлен ПОСЛЕ boot(): boot() синхронно зовёт "
        "fetchT(), дефолт которого ms=FETCH_TIMEOUT_MS → TDZ, запуск падает "
        "«Cannot access 'FETCH_TIMEOUT_MS' before initialization»."
    )


def test_fetch_timeout_declared_exactly_once():
    # ровно одно объявление — иначе редекларация const бросит SyntaxError на весь скрипт
    assert len(re.findall(r"\bconst\s+FETCH_TIMEOUT_MS\b", _html())) == 1


def test_boot_calls_fetcht_for_auth():
    # фиксируем инвариант, из-за которого важен порядок: boot СИНХРОННО дергает
    # fetchT на /auth (дефолтный ms=FETCH_TIMEOUT_MS)
    html = _html()
    i_boot = html.find("function boot()")
    seg = html[i_boot:i_boot + 900]
    assert "fetchT('/api/miniapp/auth'" in seg or 'fetchT("/api/miniapp/auth"' in seg, \
        "boot() больше не зовёт fetchT('/auth') — обнови инвариант теста, если это намеренно"

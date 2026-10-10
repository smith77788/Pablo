"""Права админа в мини-аппе должны работать и в роли web.

Сессионный вход по секретной фразе живёт в БД (schema_v181), а процесс держит
лишь кэш, который наполняет фоновый цикл run_session_admin_refresh. Гейт роли
выключает ВСЕ фоновые циклы в роли web — а решение о доступе принимает именно
web-процесс: _is_admin мини-аппа и проверка тарифа читают этот самый кэш.
С гейтом кэш в web оставался пустым навсегда: вошедший админ не получал прав
в мини-аппе вообще, работали только постоянные ADMIN_IDS из окружения.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _spawn_line(service: str) -> str:
    for line in _src("main.py").split("\n"):
        if f'"{service}"' in line and "_spawn(" in line or (
                f'"{service}"' in line and "resilient(" in line):
            return line.strip()
    raise AssertionError(f"сервис {service} не запускается в main.py")


def test_session_admin_refresh_is_not_role_gated():
    line = _spawn_line("session_admin_refresh")
    assert "_web_resilient(" in line, (
        "обновление кэша админ-сессий обязано идти мимо гейта роли: "
        "в роли web фоновые циклы не стартуют, и кэш прав остаётся пустым")
    assert "_spawn(_resilient(" not in line, "гейт роли снова выключает кэш прав"


def test_web_role_gate_is_still_in_place_for_real_background_loops():
    """Обход гейта — точечный, а не отмена разделения ролей."""
    src = _src("main.py")
    assert '_ROLE == "web"' in src
    # Настоящие фоновые движки по-прежнему идут через _resilient.
    for service in ("op_worker", "account_monitor", "trust_engine"):
        line = _spawn_line(service)
        assert "_web_resilient(" not in line, (
            f"{service} — фоновый движок, он обязан отключаться в роли web")


def test_mini_app_admin_check_reads_that_cache():
    """Если источник прав в мини-аппе сменится, этот тест должен об этом узнать."""
    api = _src(os.path.join("services", "mini_app_api.py"))
    m = re.search(r"def _is_admin\(.*?\n(.*?)\n\ndef ", api, re.DOTALL)
    assert m, "_is_admin не найден в mini_app_api"
    body = m.group(1)
    assert "_session_admins" in body, (
        "мини-апп больше не читает кэш админ-сессий — проверьте, что права "
        "в роли web вообще откуда-то берутся")
    assert "ADMIN_IDS" in api


def test_subscription_gate_reads_the_same_cache():
    sub = _src(os.path.join("bot", "utils", "subscription.py"))
    assert "_session_admins" in sub, (
        "проверка тарифа читает тот же кэш — он тоже пуст в роли web без цикла")

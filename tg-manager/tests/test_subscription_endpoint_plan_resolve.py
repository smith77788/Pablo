"""Регрессия: эндпоинт /api/miniapp/subscription теряет ручную выдачу тарифа.

Жалоба (скриншот): «я админ и имею подписку — мини-апп показывает Free».

Причина — хендлер `subscription` резолвил тариф как `plan = get_plan(); if not
plan: fallback на platform_users`. Но get_plan возвращает "free" (истинную
строку), поэтому фолбэк на platform_users НЕ срабатывал: тариф, выданный админом
только в platform_users (enterprise, без активной строки в subscriptions),
показывался как Free. Дашборд эту же ловушку уже чинит через `_resolve_best_plan`
(см. test_dashboard_plan_resolve.py) — эндпоинт subscription должен делать так же.

Тест статический (как соседние miniapp-эндпоинт-тесты): фиксирует, что хендлер
берёт наивысший тариф среди источников и НЕ полагается на мёртвую ветку
`if not plan:`.
"""
from __future__ import annotations

import inspect
import re

from services import mini_app_api


def _subscription_body() -> str:
    src = inspect.getsource(mini_app_api)
    m = re.search(r"async def subscription\(.*?\n(.*?)\n    async def ", src, re.DOTALL)
    assert m, "subscription handler not found"
    return m.group(1)


def _code_only(body: str) -> str:
    """Тело хендлера без строк-комментариев — чтобы проверять КОД, а не пояснения
    (в комментариях специально упоминается старая ветка)."""
    lines = []
    for ln in body.splitlines():
        stripped = ln.lstrip()
        if stripped.startswith("#"):
            continue
        lines.append(ln)
    return "\n".join(lines)


def test_subscription_uses_best_plan_resolver():
    body = _subscription_body()
    assert "_resolve_best_plan(" in body, (
        "subscription должен резолвить тариф через _resolve_best_plan "
        "(наивысший среди get_plan/subscriptions/platform_users)"
    )


def test_subscription_consults_platform_users():
    body = _subscription_body()
    assert "platform_users" in body, (
        "subscription должен читать ручную выдачу из platform_users"
    )


def test_subscription_has_no_dead_free_fallback():
    """`if not plan:`-фолбэк — мёртвая ветка (get_plan даёт 'free', а не пустоту).
    Его наличие означает возврат старого бага «enterprise виден как Free»."""
    body = _code_only(_subscription_body())
    assert "if not plan:" not in body, (
        "мёртвая ветка `if not plan:` возвращает баг: platform_users не читается, "
        "когда get_plan вернул истинную строку 'free'"
    )

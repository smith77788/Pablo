"""Proxy hygiene — безопасная гигиена пула прокси для больших сеток.

Три задачи оператора с сотнями прокси: (1) НЕ удалить прокси, назначенный
аккаунту (FK `tg_accounts.proxy_id` = ON DELETE SET NULL → аккаунт молча уходит
НАПРЯМУЮ → AUTH_KEY_DUPLICATED, самый дорогой класс багов, см. CLAUDE.md);
(2) массово вычистить подтверждённо-мёртвые НЕназначенные прокси; (3) выгрузить
список для аудита без утечки кредов.

Здесь — только чистые решающие функции (тестируются без БД). Сами запросы/эффект
— в mini_app_api (delete_proxy guard, proxy_cleanup_dead, proxy_export).
"""
from __future__ import annotations

import re


def proxy_is_dead(is_active, is_alive) -> bool:
    """Мёртвый = подтверждён пробой как недоступный (`is_alive IS FALSE`) ИЛИ
    явно деактивирован (`is_active IS FALSE`).

    Важно: `is_alive IS NULL` (никогда не проверяли) — НЕ мёртвый. Неизвестность
    не равна смерти: не удаляем непроверенные прокси автоматически."""
    return (is_active is False) or (is_alive is False)


def can_delete_safely(assigned_count) -> bool:
    """Удалять прокси безопасно ТОЛЬКО если он не назначен ни одному аккаунту.
    Иначе удаление обнулит proxy_id аккаунта (ON DELETE SET NULL) и аккаунт уйдёт
    напрямую с домашнего IP → рассинхрон IP → AUTH_KEY_DUPLICATED."""
    try:
        return int(assigned_count or 0) == 0
    except (TypeError, ValueError):
        return False


def is_dead_removable(assigned_count, is_active, is_alive) -> bool:
    """Кандидат на авто-вычистку: НЕназначен И подтверждён мёртвым пробой.

    Требуем именно `is_alive IS FALSE` (а не просто деактивацию) — чтобы «очистить
    мёртвые» удаляло только реально не отвечающие прокси после проверки, а не
    временно выключенные оператором."""
    return can_delete_safely(assigned_count) and (is_alive is False)


def mask_proxy_url(url) -> str:
    """Замаскировать креды для экспорта: scheme://user:pass@host:port →
    scheme://***@host:port. Хост/порт оставляем (нужны для аудита), логин/пароль —
    нет (утечка секретов в CSV недопустима)."""
    s = str(url or "")
    if not s:
        return ""
    # прячем всё между '//' и '@' (user:pass)
    return re.sub(r"(://)[^@/]+@", r"\1***@", s)

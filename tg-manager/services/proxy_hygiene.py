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


async def cleanup_dead_proxies(pool, owner_id: int) -> dict:
    """Очистка мёртвых прокси: безопасное удаление подтверждённо-мёртвых НЕназначенных.

    Использует is_dead_removable для безопасного определения кандидатов.
    Удаляет ТОЛЬКО прокси, которые:
    1. Подтверждённо мёртвые (is_alive = FALSE)
    2. Не назначены ни одному аккаунту (избегаем ON DELETE SET NULL)

    Возвращает {removed_count, removed_ids, skipped_assigned, errors}
    """
    result = {
        "removed_count": 0,
        "removed_ids": [],
        "skipped_assigned": 0,
        "errors": [],
    }
    try:
        rows = await pool.fetch(
            """SELECT p.id, p.is_active, p.is_alive,
                      (SELECT COUNT(*) FROM tg_accounts a
                       WHERE a.proxy_id = p.id AND a.is_active = TRUE) AS assigned_count
               FROM user_proxies p
               WHERE p.owner_id = $1""",
            owner_id,
        )
    except Exception as e:
        result["errors"].append(f"fetch failed: {e}")
        return result

    for row in rows:
        assigned_count = row["assigned_count"] or 0
        if not is_dead_removable(assigned_count, row["is_active"], row["is_alive"]):
            if assigned_count > 0:
                result["skipped_assigned"] += 1
            continue
        try:
            await pool.execute(
                "DELETE FROM user_proxies WHERE id = $1 AND owner_id = $2",
                row["id"], owner_id,
            )
            result["removed_ids"].append(row["id"])
            result["removed_count"] += 1
        except Exception as e:
            result["errors"].append(f"delete proxy {row['id']}: {e}")

    return result


async def export_proxies(pool, owner_id: int, fmt: str = "csv") -> dict:
    """Экспорт прокси с маскированием кредов.

    fmt: 'csv' | 'json' | 'list'
    - csv: scheme://***@host:port per line (для ручного импорта)
    - json: [{id, masked_url, geo_country, is_active, is_alive}]
    - list: простой список masked_url

    Возвращает {format, count, data: str|list}
    """
    try:
        rows = await pool.fetch(
            """SELECT id, proxy_url, geo_country, is_active, is_alive
               FROM user_proxies
               WHERE owner_id = $1
               ORDER BY id""",
            owner_id,
        )
    except Exception as e:
        return {"format": fmt, "count": 0, "data": "", "error": str(e)}

    records = []
    for row in rows:
        from services.token_vault import decrypt_token

        plain_url = decrypt_token(row["proxy_url"] or "")
        masked = mask_proxy_url(plain_url)
        records.append({
            "id": row["id"],
            "masked_url": masked,
            "geo_country": row.get("geo_country", ""),
            "is_active": row["is_active"],
            "is_alive": row["is_alive"],
        })

    if fmt == "json":
        import json
        return {"format": "json", "count": len(records), "data": records}
    elif fmt == "list":
        data = [r["masked_url"] for r in records if r["masked_url"]]
        return {"format": "list", "count": len(data), "data": data}
    else:
        lines = [r["masked_url"] for r in records if r["masked_url"]]
        return {"format": "csv", "count": len(lines), "data": "\n".join(lines)}

"""Планировщик кампании: цель → ёмкостная раскладка (шов «Цель → план»).

Мета-контроллер верхнего слоя. Оператор задаёт ИСХОД («+1000 участников в канал
X за 5 дней»), планировщик считает по РЕАЛЬНОЙ ёмкости флота (та же математика,
что в модели «Экономика флота»): безопасная дневная ёмкость = Σ лимитов
аккаунтов; с поправкой на долю вступивших по методу инвайта. Отвечает: реально
ли к сроку, сколько в день, на аккаунт, чего не хватает — и отдаёт готовый
конфиг для исполнителя (шов «Инвайт → Welcome»).

Без «магии ИИ»: чистая арифметика ёмкости под ограничением. Ядро compute_plan
детерминировано и тестируется без БД.
"""
from __future__ import annotations

import math

# Доля приглашённых, кто реально становится участником, по методу инвайта.
# direct — часть режет приватность; admin (промоут-трюк) — почти все; link —
# самостоятельное вступление, конверсия низкая.
JOIN_YIELD = {"direct": 0.6, "admin": 0.85, "link": 0.15}


def compute_plan(goal_count: int, deadline_days: int, daily_capacity: int,
                 n_accounts: int, method: str = "direct") -> dict:
    """Чистое ядро. Возвращает раскладку и вердикт достижимости."""
    goal = max(1, int(goal_count))
    deadline = max(1, int(deadline_days))
    cap = max(0, int(daily_capacity))
    n = max(0, int(n_accounts))
    y = JOIN_YIELD.get(method, JOIN_YIELD["direct"])

    invites_needed = math.ceil(goal / y)
    needed_daily = math.ceil(invites_needed / deadline)      # чтобы успеть к сроку
    days_at_capacity = math.ceil(invites_needed / cap) if cap else None
    feasible = cap > 0 and days_at_capacity is not None and days_at_capacity <= deadline

    per_acc_cap = math.floor(cap / n) if n else 0
    # Сколько инвайтов/аккаунт/день нужно, чтобы уложиться к сроку.
    per_acc_needed = math.ceil(needed_daily / n) if n else 0
    # Если не хватает ёмкости — сколько ещё аккаунтов той же силы нужно.
    avg_per_acc = (cap / n) if n else 0
    extra_accounts = 0
    if not feasible and avg_per_acc > 0:
        total_needed = math.ceil(needed_daily / avg_per_acc)
        extra_accounts = max(0, total_needed - n)

    # Рекомендованный лимит на аккаунт для запуска: минимум из «нужно к сроку» и
    # «безопасно тянет», но не выше потолка одного прохода.
    rec_per_account = max(1, min(per_acc_needed or per_acc_cap or 1, per_acc_cap or per_acc_needed or 1, 50)) if n else 0

    verdict = _verdict(feasible, days_at_capacity, deadline, cap, n, extra_accounts)
    return {
        "goal": goal, "deadline_days": deadline, "method": method,
        "join_yield": y,
        "fleet_accounts": n,
        "daily_capacity": cap,
        "invites_needed": invites_needed,
        "needed_daily": needed_daily,
        "days_at_capacity": days_at_capacity,
        "feasible": feasible,
        "per_account_cap": per_acc_cap,
        "per_account_needed": per_acc_needed,
        "recommended_per_account": rec_per_account,
        "extra_accounts": extra_accounts,
        "verdict": verdict,
        # Готовый конфиг для экрана инвайта (шов «Инвайт → Welcome»).
        "launch": {"invite_method": method, "per_account_limit": rec_per_account,
                   "pace": "auto"},
    }


def _verdict(feasible, days, deadline, cap, n, extra) -> str:
    if n == 0:
        return "Нет активных аккаунтов с сессией — сначала подключите/почините флот."
    if cap == 0:
        return "Флот пока без безопасной ёмкости (холодный старт/флуды) — прогрейте аккаунты."
    if feasible:
        return (f"✅ Достижимо: при текущей ёмкости уложитесь за ~{days} дн. "
                f"(до срока {deadline}).")
    parts = [f"⚠️ К сроку не хватает ёмкости: при текущем флоте нужно ~{days} дн."]
    if extra:
        parts.append(f"Добавьте ~{extra} аккаунт(ов) той же силы")
    parts.append("либо прогрейте флот (поднимет лимиты), либо сдвиньте срок.")
    return " ".join(parts)


async def plan(pool, owner_id: int, goal_count: int, deadline_days: int,
               method: str = "direct") -> dict:
    """Собрать ёмкость флота и посчитать план."""
    accs = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active "
        "AND session_str IS NOT NULL", owner_id)
    acc_ids = [int(r["id"]) for r in (accs or [])]
    daily = 0
    from services import flood_engine
    for aid in acc_ids:
        try:
            d = await flood_engine.recommended_daily_limit(pool, aid)
            daily += int(d.get("limit", 0) or 0)
        except Exception:
            pass
    return compute_plan(goal_count, deadline_days, daily, len(acc_ids), method)

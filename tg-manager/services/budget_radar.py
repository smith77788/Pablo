"""Экономика флота: где прямо сейчас утекает бюджет на прокси и аккаунты.

БОЛЬ. Главная статья расхода в больших сетках — расходники: прокси (платим
помесячно за каждый) и аккаунты (покупка + активация). Конкуренты показывают
«здоровье» и «статус», но НИКТО не переводит состояние флота в деньги. В итоге
владелец месяцами платит за прокси, привязанные к мёртвым аккаунтам, за пустые
прокси без аккаунтов и за регистрации, сгоревшие в первые дни, — и не видит
этого. Деньги утекают тихо.

Этот модуль — «радар утечки бюджета»: сканирует флот и выдаёт ранжированный
список КОНКРЕТНЫХ утечек в деньгах, по каждой — готовое действие. Работает без
ручного ввода цен: берёт средние ставки из platform_settings (или разумные
дефолты), поэтому показывает цифру сразу, а не после недели заведения данных.

Здесь только чистые функции классификации/подсчёта (тестируются без БД) плюс
async scan(), собирающий отчёт из УЖЕ существующих таблиц (user_proxies,
tg_accounts, operation_audit) — без изменений схемы. Это не обход защит Telegram,
а гигиена расходов: не платить за то, что не приносит действий.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# Статусы «аккаунт мёртв безвозвратно» — деньги на его прокси уже не окупятся.
# cooldown/spamblock/warming НЕ входят: это временно, аккаунт ещё вернётся.
DEAD_STATUSES = ("banned", "deleted", "session_expired")

# Риск-действия = «аккаунт реально работает» (согласовано с account_budget).
# Если по ним нет активности N дней — прокси под аккаунтом простаивает.
_ACTIVE_ACTIONS = (
    "post", "publish", "join", "leave", "dm", "invite", "report",
    "boost", "reaction", "view", "create_channel", "create_group",
    "profile", "story",
)

# Дефолтные ставки. Переопределяются через platform_setting (см. get_cost_model).
# Намеренно консервативные и в «нейтральной» валюте — оператор подгонит под себя.
DEFAULTS: dict[str, Any] = {
    "proxy_monthly": 60.0,   # цена одного прокси в месяц
    "account_unit": 25.0,    # цена одного аккаунта (покупка + активация)
    "currency": "₽",
    "idle_days": 7,          # нет действий столько дней → аккаунт «простаивает»
    "fast_death_days": 3,    # умер в первые столько дней жизни → «сгорел»
    "burn_window_days": 30,  # окно, за которое считаем сгоревшие регистрации
}

# Приоритет утечек — от «горит» к «мелкой».
LEAK_ORDER = ("dead_proxy_assigned", "dead_weight", "idle", "stale")

_LEAK_LABEL = {
    "dead_proxy_assigned": "☠️ мёртвый прокси под аккаунтом",
    "dead_weight": "🪦 прокси только на мёртвых аккаунтах",
    "idle": "💤 пустой прокси (нет аккаунтов)",
    "stale": "🐌 прокси на простаивающих аккаунтах",
}

_LEAK_FIX = {
    "dead_proxy_assigned": (
        "Заменить прокси у аккаунта (failover на резервный) — иначе аккаунт "
        "уйдёт с домашнего IP и словит AUTH_KEY_DUPLICATED."
    ),
    "dead_weight": "Отвязать и удалить прокси, либо переназначить живому аккаунту.",
    "idle": "Назначить прокси живому аккаунту или удалить из пула.",
    "stale": "Аккаунты не работают — запустить прогрев/операции или снять прокси.",
}


# ── Чистые функции (без БД) ────────────────────────────────────────────────────

def _num(raw: Any, default: float) -> float:
    """Безопасный парс числа из настройки. Пусто/мусор/отрицательное → default."""
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)
    return v if v >= 0 else float(default)


def fmt_money(x: float, currency: str) -> str:
    """Деньги для экрана: целые — без копеек, дробные — с двумя знаками."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        x = 0.0
    if abs(x - round(x)) < 0.005:
        return f"{int(round(x))} {currency}"
    return f"{x:.2f} {currency}"


def _short_proxy(label: Any, url: Any) -> str:
    """Короткое имя прокси для экрана: метка, иначе host:port без кредов."""
    lbl = str(label or "").strip()
    if lbl:
        return lbl[:32]
    s = str(url or "").strip()
    if not s:
        return "—"
    # host[:port] между '@' (если есть креды) и концом; креды не показываем
    m = re.search(r"@([^/@\s]+)", s)
    if m:
        return m.group(1)[:32]
    m = re.search(r"//([^/@\s]+)", s)
    return (m.group(1) if m else s)[:32]


def classify_proxy(row: dict) -> Optional[str]:
    """Тип утечки для строки прокси, либо None если прокси окупается.

    row: is_alive, assigned_total, assigned_dead, assigned_idle.
      assigned_total — сколько аккаунтов на прокси,
      assigned_dead  — из них мёртвых (banned/deleted/session_expired/неактивных),
      assigned_idle  — живых, но без риск-действий N дней.
    """
    total = int(row.get("assigned_total") or 0)
    dead = int(row.get("assigned_dead") or 0)
    idle = int(row.get("assigned_idle") or 0)
    is_alive = row.get("is_alive")
    live = total - dead  # живые (не мёртвые) аккаунты

    # 1) Прокси подтверждён мёртвым, но к нему привязан аккаунт: и опасно (аккаунт
    #    уйдёт напрямую → AUTH_KEY), и деньги на ветер. Высший приоритет.
    if is_alive is False and total > 0:
        return "dead_proxy_assigned"
    # 2) Аккаунты есть, но все мёртвые — платим за прокси, который никого не везёт.
    if total > 0 and live == 0:
        return "dead_weight"
    # 3) Совсем пустой прокси — платим ни за что.
    if total == 0:
        return "idle"
    # 4) Живые аккаунты есть, но ВСЕ простаивают ≥N дней.
    if live > 0 and idle >= live:
        return "stale"
    # Иначе есть хотя бы один работающий живой аккаунт — прокси окупается.
    return None


def leak_from_row(row: dict, cost: dict) -> Optional[dict]:
    """Собрать запись утечки из строки прокси, либо None."""
    kind = classify_proxy(row)
    if kind is None:
        return None
    return {
        "kind": kind,
        "label": _LEAK_LABEL[kind],
        "fix": _LEAK_FIX[kind],
        "proxy_id": int(row.get("id") or 0),
        "proxy": _short_proxy(row.get("label"), row.get("proxy_url")),
        "assigned_total": int(row.get("assigned_total") or 0),
        "assigned_dead": int(row.get("assigned_dead") or 0),
        # Каждый утекающий прокси = целая месячная строка расхода без отдачи.
        "monthly_waste": round(float(cost.get("proxy_monthly") or 0.0), 2),
    }


def rank_leaks(leaks: Iterable[dict]) -> list[dict]:
    """Сортировка утечек: сначала по приоритету типа, внутри — по деньгам вниз."""
    order = {k: i for i, k in enumerate(LEAK_ORDER)}
    return sorted(
        leaks,
        key=lambda x: (order.get(x.get("kind"), 99), -float(x.get("monthly_waste") or 0)),
    )


def total_monthly_waste(leaks: Iterable[dict]) -> float:
    return round(sum(float(l.get("monthly_waste") or 0) for l in leaks), 2)


def summarize(leaks: Iterable[dict]) -> dict[str, int]:
    """Счётчики утечек по типам (для сводки на экране)."""
    leaks = list(leaks)
    return {k: sum(1 for l in leaks if l.get("kind") == k) for k in LEAK_ORDER}


# ── Async: сбор отчёта из БД ────────────────────────────────────────────────────

_PROXY_STATS_SQL = """
SELECT p.id, p.label, p.proxy_url, p.is_active, p.is_alive,
       COALESCE(a.total, 0) AS assigned_total,
       COALESCE(a.dead, 0)  AS assigned_dead,
       COALESCE(a.idle, 0)  AS assigned_idle
FROM user_proxies p
LEFT JOIN (
    SELECT ta.proxy_id,
           COUNT(*) AS total,
           COUNT(*) FILTER (
               WHERE ta.is_active IS FALSE OR ta.acc_status = ANY($3::text[])
           ) AS dead,
           COUNT(*) FILTER (
               WHERE ta.is_active IS TRUE
                 AND (ta.acc_status IS NULL OR ta.acc_status <> ALL($3::text[]))
                 AND NOT EXISTS (
                     SELECT 1 FROM operation_audit oa
                     WHERE oa.account_id = ta.id
                       AND oa.action = ANY($4::text[])
                       AND oa.occurred_at > now() - make_interval(days => $2)
                 )
           ) AS idle
    FROM tg_accounts ta
    WHERE ta.owner_id = $1 AND ta.proxy_id IS NOT NULL
    GROUP BY ta.proxy_id
) a ON a.proxy_id = p.id
WHERE p.owner_id = $1
"""

_BURNED_SQL = """
SELECT COUNT(*) AS n
FROM tg_accounts
WHERE owner_id = $1
  AND (is_active IS FALSE OR acc_status = ANY($4::text[]))
  AND created_at > now() - make_interval(days => $3)
  AND (last_used IS NULL OR last_used <= created_at + make_interval(days => $2))
"""


async def get_cost_model(pool) -> dict:
    """Ставки расходов: platform_setting или дефолт. Никогда не падает."""
    cost = dict(DEFAULTS)
    try:
        from database.db import get_platform_setting
        cost["proxy_monthly"] = _num(
            await get_platform_setting(pool, "budget_proxy_monthly_cost", ""),
            DEFAULTS["proxy_monthly"])
        cost["account_unit"] = _num(
            await get_platform_setting(pool, "budget_account_cost", ""),
            DEFAULTS["account_unit"])
        cur = (await get_platform_setting(pool, "budget_currency", "")).strip()
        cost["currency"] = cur or DEFAULTS["currency"]
        cost["idle_days"] = int(_num(
            await get_platform_setting(pool, "budget_idle_days", ""),
            DEFAULTS["idle_days"]))
        cost["fast_death_days"] = int(_num(
            await get_platform_setting(pool, "budget_fast_death_days", ""),
            DEFAULTS["fast_death_days"]))
    except Exception:
        log.debug("budget_radar.get_cost_model: settings read failed", exc_info=True)
    return cost


async def _burned_accounts(pool, owner_id: int, cost: dict) -> dict:
    """Сгоревшие регистрации: созданы в окне и умерли в первые fast_death_days."""
    try:
        n = await pool.fetchval(
            _BURNED_SQL, owner_id, int(cost["fast_death_days"]),
            int(cost["burn_window_days"]), list(DEAD_STATUSES))
        n = int(n or 0)
    except Exception:
        log.debug("budget_radar._burned_accounts: query failed", exc_info=True)
        n = 0
    return {
        "count": n,
        "days": int(cost["fast_death_days"]),
        "window": int(cost["burn_window_days"]),
        "cost": round(n * float(cost.get("account_unit") or 0.0), 2),
    }


async def scan(pool, owner_id: int, cost: Optional[dict] = None) -> dict:
    """Отчёт «Экономика флота» для владельца. Fail-soft: сбой → пустой отчёт."""
    if cost is None:
        cost = await get_cost_model(pool)
    try:
        rows = await pool.fetch(
            _PROXY_STATS_SQL, owner_id, int(cost["idle_days"]),
            list(DEAD_STATUSES), list(_ACTIVE_ACTIONS))
    except Exception:
        log.debug("budget_radar.scan: proxy stats failed owner=%s", owner_id,
                  exc_info=True)
        rows = []

    leaks: list[dict] = []
    for r in rows:
        leak = leak_from_row(dict(r), cost)
        if leak:
            leaks.append(leak)
    leaks = rank_leaks(leaks)

    burned = await _burned_accounts(pool, owner_id, cost)
    monthly = total_monthly_waste(leaks)
    return {
        "currency": cost["currency"],
        "cost": cost,
        "proxies_total": len(rows),
        "leaks": leaks,
        "leak_count": len(leaks),
        "monthly_waste": monthly,
        "annual_waste": round(monthly * 12, 2),
        "by_kind": summarize(leaks),
        "burned": burned,
    }

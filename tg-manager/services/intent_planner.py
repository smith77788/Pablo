"""Intent Planner — Plan Builder, Strategy Engine, Execution Forecast (Epoch IV)."""

from __future__ import annotations

import asyncpg

from services.presence_planner import estimate_duration_minutes
from services.geo_data import GEO_PRESETS

# ─── Intent classification ────────────────────────────────────────────────────

_INTENT_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "presence",
        [
            "присутствие",
            "presence",
            "городах",
            "городов",
            "регион",
            "geo",
            "глобал",
            "deploy",
        ],
    ),
    ("strike", ["strike", "жалоба", "репорт", "report", "страйк", "ban", "удалить"]),
    (
        "network",
        ["сеть", "network", "инфраструктур", "разверн", "create network", "нов"],
    ),
    (
        "sync",
        ["синхронизир", "sync", "обновить все", "привести", "шаблону", "синхронизац"],
    ),
    (
        "audit",
        [
            "проверить",
            "audit",
            "аудит",
            "здоровье",
            "health",
            "состояние",
            "диагностик",
        ],
    ),
    (
        "growth",
        ["масштаб", "growth", "расширить", "усилить", "увелич", "развить", "scale"],
    ),
    (
        "visibility",
        ["видимость", "visibility", "поиск", "seo", "позиции", "ранжировани", "рейтинг", "ranking", "усилить присутствие"],
    ),
]


def classify_intent(description: str) -> str:
    low = description.lower()
    for intent_type, keywords in _INTENT_PATTERNS:
        if any(kw in low for kw in keywords):
            return intent_type
    return "custom"


# ─── Geo preset & asset detection ────────────────────────────────────────────

_GEO_KEYWORDS: dict[str, list[str]] = {
    "eu_capitals": ["европ", "europe", "евросоюз", "eu capital"],
    "world_capitals": ["мир", "world", "глобальн", "global", "world capital"],
    "cis": ["снг", "cis", "постсоветск", "снг-страны"],
    "dach": ["германи", "german", "austria", "австри", "швейцар", "dach"],
    "latam": ["латин", "latin", "brazil", "бразил", "аргентин", "latam"],
    "russia": ["россия", "russia", "рф", "russian", "росс"],
    "ukraine": ["украин", "ukraine", "укр"],
    "belarus": ["беларус", "belarus", "белорус"],
    "tier1": ["tier1", "tier-1", "крупных", "major cities", "мегаполис"],
}


def detect_geo_preset(description: str) -> str:
    low = description.lower()
    for preset, keywords in _GEO_KEYWORDS.items():
        if preset in GEO_PRESETS and any(kw in low for kw in keywords):
            return preset
    return "eu_capitals"


def detect_asset_type(description: str) -> str:
    low = description.lower()
    if any(kw in low for kw in ["бот", "bot", "боты", "bots"]):
        return "bot"
    if any(kw in low for kw in ["групп", "group", "чат", "chat"]):
        return "group"
    if any(kw in low for kw in ["полный пакет", "full package"]):
        return "full_package"
    if any(kw in low for kw in ["пакет", "package"]):
        return "package"
    return "channel"


# ─── Resource assessment ──────────────────────────────────────────────────────


async def assess_resources(pool: asyncpg.Pool, owner_id: int) -> dict:
    acc_row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt, COALESCE(AVG(trust_score), 0.5) AS avg_trust "
        "FROM tg_accounts WHERE owner_id=$1 AND trust_score > 0.3",
        owner_id,
    )
    proxy_row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM user_proxies WHERE owner_id=$1 AND is_active=TRUE",
        owner_id,
    )
    ops_row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM operation_queue "
        "WHERE owner_id=$1 AND status IN ('pending','running')",
        owner_id,
    )
    gp_row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM global_presence_plans "
        "WHERE owner_id=$1 AND status NOT IN ('done','failed','cancelled')",
        owner_id,
    )
    return {
        "accounts_available": int(acc_row["cnt"] or 0),
        "accounts_avg_trust": round(float(acc_row["avg_trust"] or 0.5), 2),
        "proxies_available": int(proxy_row["cnt"] or 0),
        "active_operations": int(ops_row["cnt"] or 0),
        "active_gp_plans": int(gp_row["cnt"] or 0),
    }


# ─── Plan Builder ─────────────────────────────────────────────────────────────


async def build_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    intent_type: str,
    description: str,
    resources: dict,
) -> dict:
    builders = {
        "presence": _build_presence_plan,
        "network": _build_network_plan,
        "audit": _build_audit_plan,
        "sync": _build_sync_plan,
        "growth": _build_growth_plan,
        "strike": _build_strike_plan,
        "visibility": _build_visibility_plan,
    }
    builder = builders.get(intent_type, _build_custom_plan)
    return await builder(pool, owner_id, description, resources)


async def _build_presence_plan(pool, owner_id, description, resources):
    geo_preset = detect_geo_preset(description)
    asset_type = detect_asset_type(description)

    preset_info = GEO_PRESETS.get(geo_preset) or GEO_PRESETS["eu_capitals"]
    geo_label = preset_info["label"]
    n_targets = preset_info["count"]

    asset_labels = {
        "channel": "Каналы",
        "group": "Группы",
        "bot": "Боты",
        "package": "Пакет (каналы + группы)",
        "full_package": "Полный пакет (каналы + группы + боты)",
    }
    asset_label = asset_labels.get(asset_type, "Каналы")

    # Pick best accounts
    n_accounts_needed = max(
        1, min(resources["accounts_available"], max(1, n_targets // 15))
    )
    acc_rows = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND trust_score > 0.3 "
        "ORDER BY trust_score DESC LIMIT $2",
        owner_id,
        n_accounts_needed,
    )
    account_ids = [r["id"] for r in acc_rows]

    risks: list[str] = []
    if n_targets > 100:
        risks.append(f"⚠️ {n_targets} объектов — высокая нагрузка на аккаунты")
    if len(account_ids) == 0:
        risks.append("🚫 Нет доступных аккаунтов — добавьте аккаунты")
    elif len(account_ids) < 3:
        risks.append("⚠️ Мало аккаунтов — операции займут больше времени")
    if resources["proxies_available"] < n_targets // 10:
        risks.append("⚠️ Рекомендуется добавить прокси для ускорения")
    if resources["active_operations"] > 5:
        risks.append("⚠️ Много активных операций — возможны задержки")

    name_pattern = "{{CITY_NAME}} News"
    username_pattern = "news_{{CITY_SLUG}}"

    return {
        "intent_type": "presence",
        "goal": f"Создать {asset_label.lower()} — {geo_label}",
        "geo_preset": geo_preset,
        "geo_label": geo_label,
        "n_targets": n_targets,
        "asset_type": asset_type,
        "asset_label": asset_label,
        "name_pattern": name_pattern,
        "username_pattern": username_pattern,
        "n_accounts_available": resources["accounts_available"],
        "n_accounts_selected": len(account_ids),
        "account_ids": account_ids,
        "n_proxies_available": resources["proxies_available"],
        "steps": [
            f"1. Геопресет: {geo_label} ({n_targets} объектов)",
            f"2. Тип актива: {asset_label}",
            f"3. Паттерн: <code>{name_pattern}</code>",
            f"4. Username: <code>{username_pattern}</code>",
            f"5. Аккаунты: {len(account_ids)} из {resources['accounts_available']} доступных",
            "6. Постановка в очередь через operation_queue",
        ],
        "risks": risks if risks else ["✅ Риски в норме"],
        "executable": len(account_ids) > 0,
        "action": "execute_gp",
        "navigate_to": "gp_factory",
    }


async def _build_network_plan(pool, owner_id, description, resources):
    asset_type = detect_asset_type(description)
    asset_labels = {"channel": "каналов", "bot": "ботов", "group": "групп"}
    asset_label = asset_labels.get(asset_type, "каналов")

    n_to_create = 10  # default
    import re

    m = re.search(
        r"(\d+)\s*(каналов|ботов|групп|channel|bot|group)", description.lower()
    )
    if m:
        n_to_create = min(int(m.group(1)), 500)

    risks: list[str] = []
    if resources["accounts_available"] == 0:
        risks.append("🚫 Нет доступных аккаунтов")
    if n_to_create > 50:
        risks.append(f"⚠️ Создание {n_to_create} объектов займёт значительное время")

    return {
        "intent_type": "network",
        "goal": f"Создать сеть из {n_to_create} {asset_label}",
        "n_targets": n_to_create,
        "asset_type": asset_type,
        "asset_label": asset_label.capitalize(),
        "n_accounts_available": resources["accounts_available"],
        "steps": [
            f"1. Выбор типа: {asset_label}",
            f"2. Количество: {n_to_create}",
            f"3. Аккаунты: {resources['accounts_available']} доступно",
            "4. Запуск через Factory",
        ],
        "risks": risks if risks else ["✅ Риски в норме"],
        "executable": False,
        "action": "navigate",
        "navigate_to": "factory",
    }


async def _build_audit_plan(pool, owner_id, description, resources):
    bots_cnt = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE owner_id=$1", owner_id
        )
        or 0
    )
    channels_cnt = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", owner_id
        )
        or 0
    )

    return {
        "intent_type": "audit",
        "goal": "Полный аудит инфраструктуры",
        "n_bots": bots_cnt,
        "n_channels": channels_cnt,
        "n_accounts": resources["accounts_available"],
        "steps": [
            f"1. Анализ {resources['accounts_available']} аккаунтов",
            f"2. Анализ {bots_cnt} ботов + {channels_cnt} каналов",
            f"3. Проверка {resources['proxies_available']} прокси",
            "4. Выявление рисков и аномалий",
            "5. Рекомендации по устранению проблем",
        ],
        "risks": ["ℹ️ Аудит безопасен — только чтение данных"],
        "executable": True,
        "action": "run_audit",
        "navigate_to": "health_dashboard",
    }


async def _build_sync_plan(pool, owner_id, description, resources):
    channels_cnt = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", owner_id
    ) or 0
    bots_cnt = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_bots WHERE owner_id=$1", owner_id
    ) or 0
    total = channels_cnt + bots_cnt

    can_execute = channels_cnt > 0

    return {
        "intent_type": "sync",
        "goal": "Синхронизация всей инфраструктуры по шаблону",
        "n_channels": channels_cnt,
        "n_bots": bots_cnt,
        "n_total": total,
        "n_accounts_available": resources["accounts_available"],
        "steps": [
            f"1. Каналов для синхронизации: {channels_cnt}",
            f"2. Ботов для синхронизации: {bots_cnt}",
            "3. Подбор единого шаблона публикации",
            "4. Массовое обновление контента через Mass Ops",
            "5. Применение единых настроек по всем активам",
        ],
        "risks": (
            ["⚠️ Изменения затронут все активы — убедитесь в корректном шаблоне"]
            if total > 10
            else ["✅ Объём небольшой — риски минимальны"]
        ),
        "executable": can_execute,
        "action": "execute_sync" if can_execute else "navigate",
        "navigate_to": "mass_ops",
    }


async def _build_growth_plan(pool, owner_id, description, resources):
    geo_preset = detect_geo_preset(description)
    preset_info = GEO_PRESETS.get(geo_preset) or GEO_PRESETS["eu_capitals"]

    channels_cnt = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", owner_id
    ) or 0

    n_accs = resources["accounts_available"]
    can_execute = n_accs > 0 and channels_cnt > 0

    steps = [
        f"1. Анализ {channels_cnt} каналов текущего присутствия",
        f"2. Оценка ресурсов: {n_accs} аккаунтов, {resources['proxies_available']} прокси",
        "3. Подбор оптимальных аккаунтов для операции",
    ]
    if can_execute:
        target_count = min(channels_cnt, 20)
        steps.append(f"4. Массовое вступление аккаунтов в {target_count} каналов")
        steps.append("5. Обновление trust score и поведенческих метрик")
    else:
        steps.append("4. Переход к Ecosystem Factory для создания новых активов")

    return {
        "intent_type": "growth",
        "goal": "Масштабирование и усиление экосистемы",
        "geo_preset": geo_preset,
        "geo_label": preset_info["label"],
        "n_targets": max(channels_cnt, 1),
        "n_channels": channels_cnt,
        "n_accounts_available": n_accs,
        "n_proxies_available": resources["proxies_available"],
        "steps": steps,
        "risks": (
            ["⚠️ Масштабирование требует достаточного числа аккаунтов и прокси"]
            if n_accs < 3
            else ["✅ Ресурсов достаточно для масштабирования"]
        ),
        "executable": can_execute,
        "action": "execute_growth" if can_execute else "navigate",
        "navigate_to": "ecosystems",
    }


async def _build_strike_plan(pool, owner_id, description, resources):
    return {
        "intent_type": "strike",
        "goal": "Эшелонированная жалоба через STRIKE",
        "n_accounts_available": resources["accounts_available"],
        "steps": [
            "1. Выбор цели (канал/бот/пользователь)",
            "2. Подбор аккаунтов для жалоб",
            "3. Настройка интенсивности (normal/fast)",
            "4. Запуск через STRIKE Engine",
        ],
        "risks": [
            "⚠️ Используйте только против нарушителей правил",
            "⚠️ Аккаунты могут получить ограничения",
        ],
        "executable": False,
        "action": "navigate",
        "navigate_to": "strike",
    }


async def _build_visibility_plan(pool, owner_id, description, resources):
    keywords_cnt = await pool.fetchval(
        "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active=TRUE", owner_id
    ) or 0
    channels_cnt = await pool.fetchval(
        "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", owner_id
    ) or 0

    # Get avg position if rankings exist
    avg_pos = await pool.fetchval(
        """SELECT ROUND(AVG(sr.position))
           FROM tracked_keywords tk
           JOIN search_rankings sr ON sr.keyword_id = tk.id
           WHERE tk.owner_id = $1 AND tk.is_active = TRUE
             AND sr.checked_at > NOW() - INTERVAL '7 days'""",
        owner_id,
    )

    steps = [
        f"1. Анализ {keywords_cnt} ключевых слов в поиске",
        f"2. Аудит позиций {channels_cnt} каналов",
        "3. Выявление ключевых слов с низким рангом",
        "4. Анализ конкурентов по топ-позициям",
        "5. Рекомендации по SEO-оптимизации",
    ]

    risks = []
    if keywords_cnt == 0:
        risks.append("⚠️ Нет отслеживаемых ключевых слов — добавьте их для анализа")
    else:
        risks.append(f"ℹ️ Средняя позиция: {avg_pos or '—'} (по {keywords_cnt} ключевым словам)")

    return {
        "intent_type": "visibility",
        "goal": "Усиление видимости в поиске Telegram",
        "n_keywords": keywords_cnt,
        "n_channels": channels_cnt,
        "avg_position": int(avg_pos) if avg_pos else None,
        "n_accounts_available": resources["accounts_available"],
        "steps": steps,
        "risks": risks if risks else ["✅ Ключевые слова настроены"],
        "executable": True,
        "action": "run_visibility",
        "navigate_to": "ranking",
    }


async def _build_custom_plan(pool, owner_id, description, resources):
    return {
        "intent_type": "custom",
        "goal": description[:120],
        "n_accounts_available": resources["accounts_available"],
        "steps": [
            "1. Уточните цель — выберите тип намерения",
            "2. Система подберёт инструменты автоматически",
        ],
        "risks": ["ℹ️ Выберите тип намерения для получения точного плана"],
        "executable": False,
        "action": "navigate",
        "navigate_to": "main",
    }


# ─── Strategy Engine ──────────────────────────────────────────────────────────

STRATEGY_LABELS: dict[str, str] = {
    "safest": "🛡 Безопасная",
    "balanced": "⚖️ Сбалансированная",
    "fastest": "⚡ Быстрая",
    "scalable": "📈 Масштабируемая",
}

STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "safest": "Максимальные задержки, минимальная нагрузка. Дольше, но безопаснее.",
    "balanced": "Оптимальный баланс скорости и безопасности. Рекомендуется.",
    "fastest": "Минимальные задержки. Быстрее, но выше риск FloodWait.",
    "scalable": "Распределение по всем аккаунтам. Хорошо для больших объёмов.",
}


def forecast_execution(plan: dict, strategy: str = "balanced") -> dict:
    n_targets = plan.get("n_targets", 10)
    n_accounts = max(
        1, plan.get("n_accounts_selected", plan.get("n_accounts_available", 1))
    )

    safe_mode = strategy in ("safest", "balanced")
    base_minutes = estimate_duration_minutes(n_targets, safe_mode=safe_mode)

    factors = {"safest": 2.0, "balanced": 1.0, "fastest": 0.5, "scalable": 0.75}
    risk_bonuses = {"safest": -0.10, "balanced": 0.0, "fastest": 0.25, "scalable": 0.05}

    time_factor = factors.get(strategy, 1.0)
    risk_bonus = risk_bonuses.get(strategy, 0.0)

    parallel = min(n_accounts, 3)
    actual_minutes = max(1, int(base_minutes * time_factor / parallel))

    base_risk = 0.12
    if n_targets > 50:
        base_risk += 0.08
    if n_targets > 150:
        base_risk += 0.10
    if n_accounts < 2:
        base_risk += 0.12
    risk_score = round(min(0.90, max(0.05, base_risk + risk_bonus)), 2)
    success_prob = round(min(0.98, max(0.40, 1.0 - risk_score * 0.75)), 2)
    load_score = round(min(1.0, n_targets / max(1, n_accounts * 12)), 2)

    return {
        "duration_minutes": actual_minutes,
        "load_score": load_score,
        "risk_score": risk_score,
        "success_probability": success_prob,
        "strategy": strategy,
    }


# ─── Formatting ───────────────────────────────────────────────────────────────


def format_plan_card(plan: dict, forecast: dict, strategy: str) -> str:
    intent_icons = {
        "presence": "🌍",
        "network": "🕸",
        "audit": "🔍",
        "sync": "🔄",
        "growth": "📈",
        "strike": "⚔️",
        "visibility": "👁️",
        "custom": "🎯",
    }
    icon = intent_icons.get(plan.get("intent_type", "custom"), "🎯")

    lines = [
        f"<b>{icon} Цель:</b> {plan.get('goal', '—')}",
        "",
        "<b>📋 Этапы:</b>",
    ]
    for step in plan.get("steps", []):
        lines.append(f"  {step}")

    lines += ["", "<b>🔧 Ресурсы:</b>"]
    if "n_accounts_available" in plan:
        sel = plan.get("n_accounts_selected", plan["n_accounts_available"])
        lines.append(
            f"  • Аккаунтов: {plan['n_accounts_available']} (будет использовано: {sel})"
        )
    if "n_proxies_available" in plan:
        lines.append(f"  • Прокси: {plan['n_proxies_available']}")
    if "n_targets" in plan:
        lines.append(f"  • Объектов: {plan['n_targets']}")

    strat_label = STRATEGY_LABELS.get(strategy, strategy)
    lines += [
        "",
        f"<b>⏱ Прогноз [{strat_label}]:</b>",
        f"  • Время: ~{forecast['duration_minutes']} мин",
        f"  • Нагрузка: {int(forecast['load_score'] * 100)}%",
        f"  • Риск: {int(forecast['risk_score'] * 100)}%",
        f"  • Вероятность успеха: {int(forecast['success_probability'] * 100)}%",
    ]

    risks = plan.get("risks", [])
    if risks:
        lines += ["", "<b>⚠️ Оценка рисков:</b>"]
        for r in risks[:3]:
            lines.append(f"  {r}")

    return "\n".join(lines)

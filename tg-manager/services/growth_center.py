"""Мотор роста: единый взгляд на разрозненные инструменты продвижения.

Продвижение раскидано по 4 инструментам с разными op_type (накрутка, Growth
Agent, самопиар, SMM-промо). Нет единого «где мы растём и что работает». Здесь —
чистая классификация и свёртка операций роста в один отчёт по инструментам, плюс
раскладка цели по инструментам поверх `campaign_planner`. Без сети — тестируется
без БД и Telegram.
"""
from __future__ import annotations

# op_type → инструмент роста. Presence (global_presence_*, *_presence_pack) —
# отдельный модуль, здесь НЕ учитываем.
_OP_TOOL: dict[str, str] = {
    "boost_views": "boost",
    "boost_reactions": "boost",
    "boost_stories": "boost",
    "boost_subscribers": "boost",
    "boost_bot_starts": "boost",
    "niche_growth_post": "growth_agent",
    "self_promo_blast": "self_promo",
}

TOOLS: dict[str, dict] = {
    "boost": {"label": "Накрутка", "emoji": "🚀",
              "hint": "Просмотры, реакции, подписчики, сторис"},
    "growth_agent": {"label": "Growth Agent", "emoji": "🌿",
                     "hint": "Органический рост через постинг в нише"},
    "self_promo": {"label": "Self Promo", "emoji": "📣",
                   "hint": "Самопиар по своей аудитории"},
}

GROWTH_OP_TYPES: tuple[str, ...] = tuple(_OP_TOOL.keys())

_DONE = ("done", "completed")
_ACTIVE = ("running", "pending")


def classify_op(op_type: str) -> str | None:
    """Инструмент роста для op_type или None, если это не операция роста."""
    return _OP_TOOL.get(op_type or "")


# Раскладка вклада в цель по инструментам (сумма = 1.0). Инвайт — основной
# органический канал; буст — небольшой платный «толчок» (не удержание).
_MIX: tuple[tuple[str, float, str, str], ...] = (
    ("invite", 0.50, "Инвайтинг", "Массовое приглашение целевой аудитории — основной органический рост под защитой темпа."),
    ("self_promo", 0.20, "Self Promo", "Самопиар по своей аудитории — тёплый трафик, максимум конверсии."),
    ("growth_agent", 0.20, "Growth Agent", "Постинг в нишевых группах — охват новой холодной аудитории."),
    ("boost", 0.10, "Накрутка", "Платный толчок подписчиков для соц-доказательства (не удержание — подкрепляйте контентом)."),
)


def recommend_plan(goal: int, deadline_days: int, n_accounts: int,
                   daily_capacity: int = 0) -> dict:
    """Разложить цель «+N подписчиков к сроку» по инструментам роста.

    Чистая функция: вклад по _MIX + оценка достижимости инвайт-доли через
    campaign_planner.compute_plan. Возвращает {goal, deadline_days, steps[], seo,
    feasible_invite, verdict}. Каждый шаг несёт kind для дип-линка.
    """
    goal = max(1, int(goal))
    deadline_days = max(1, int(deadline_days))
    steps: list[dict] = []
    for kind, weight, label, why in _MIX:
        target = round(goal * weight)
        steps.append({"kind": kind, "label": label, "target": target,
                      "weight": weight, "why": why})
    # Достижимость инвайт-доли (основной канал) — реюз планировщика ёмкости.
    invite_target = round(goal * _MIX[0][1])
    feasible = None
    verdict = ""
    try:
        from services import campaign_planner
        cp = campaign_planner.compute_plan(
            invite_target, deadline_days, int(daily_capacity or 0),
            max(0, int(n_accounts)), method="direct")
        feasible = cp.get("feasible")
        verdict = cp.get("verdict", "")
    except Exception:
        pass
    return {
        "goal": goal, "deadline_days": deadline_days,
        "fleet_accounts": max(0, int(n_accounts)),
        "steps": steps,
        "seo": {"kind": "seo", "label": "SEO объекта",
                "why": "Разовая настройка находимости канала в поиске — усиливает все каналы притока."},
        "feasible_invite": feasible, "verdict": verdict,
    }


def summarize_growth(rows: list[dict]) -> dict:
    """Свёртка операций роста по инструментам.

    rows: [{op_type, status, done_items, total_items, created_at}].
    Возвращает {tools: {key: {...}}, totals: {...}}. Детерминирована.
    """
    tools: dict[str, dict] = {}
    for key, meta in TOOLS.items():
        tools[key] = {"key": key, "label": meta["label"], "emoji": meta["emoji"],
                      "hint": meta["hint"], "ops": 0, "done": 0, "active": 0,
                      "delivered": 0, "planned": 0, "last_at": None}
    total_ops = total_done = total_delivered = 0
    for r in rows or []:
        tool = classify_op(r.get("op_type"))
        if not tool:
            continue
        t = tools[tool]
        status = (r.get("status") or "").lower()
        t["ops"] += 1
        total_ops += 1
        if status in _DONE:
            t["done"] += 1
            total_done += 1
        elif status in _ACTIVE:
            t["active"] += 1
        t["delivered"] += int(r.get("done_items") or 0)
        t["planned"] += int(r.get("total_items") or 0)
        total_delivered += int(r.get("done_items") or 0)
        ca = r.get("created_at")
        if ca and (t["last_at"] is None or ca > t["last_at"]):
            t["last_at"] = ca
    active_tools = sum(1 for t in tools.values() if t["ops"] > 0)
    return {
        "tools": tools,
        "totals": {"ops": total_ops, "done": total_done,
                   "delivered": total_delivered, "active_tools": active_tools},
    }

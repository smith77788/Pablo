"""Мотор роста: единый взгляд на разрозненные инструменты продвижения.

Продвижение раскидано по 4 инструментам с разными op_type (накрутка, Growth
Agent, самопиар, SMM-промо). Нет единого «где мы растём и что работает». Здесь —
чистая классификация и свёртка операций роста в один отчёт по инструментам. Без
сети — тестируется без БД и Telegram. Цели роста и рычаги — services/growth_plan.py
(раньше здесь была раскладка цели по вшитым долям 50/20/20/10 — она не смотрела
ни на то, что растят, ни на то, чем владелец располагает, и удалена).
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
    "growth_agent": {"label": "Посты в нишевых группах", "emoji": "🌿",
                     "hint": "Органический рост через постинг в нише"},
    "self_promo": {"label": "Самопиар", "emoji": "📣",
                   "hint": "Самопиар по своей аудитории"},
}

GROWTH_OP_TYPES: tuple[str, ...] = tuple(_OP_TOOL.keys())

_DONE = ("done", "completed")
_ACTIVE = ("running", "pending")


def classify_op(op_type: str) -> str | None:
    """Инструмент роста для op_type или None, если это не операция роста."""
    return _OP_TOOL.get(op_type or "")


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

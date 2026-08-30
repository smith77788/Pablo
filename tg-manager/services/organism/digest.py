"""Пульс-Дайджест — единый отчёт организма о состоянии проекта.

Композиция уже существующих модулей (read-only, без сессий/флота — безопасно):
  • organism.world.snapshot — метрики флота/операций/аудитории/роста/сети/vault;
  • organism.brain.build_suggestions/narrative — рекомендации и связный обзор;
  • organism.spine.state_get/set — хранит прошлый снимок метрик → тренды н/н.

Зачем: отдельные экраны показывают срезы, но не дают ЦЕЛЬНОЙ картины «что
изменилось за неделю и что делать дальше». Дайджест сводит всё в один отчёт с
тредами (рост/падение к прошлому разу) и приоритетными действиями из мозга —
это «организм объясняет себя» и точка входа во все модули.

compose_digest — чистая функция (снимок → структура отчёта), юнит-тестируема.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Метрики, по которым считаем тренд неделя-к-неделе (плоские пути в snapshot).
_TREND_METRICS = {
    "fleet_active": ("fleet", "active"),
    "fleet_dead": ("fleet", "dead"),
    "contacts": ("graph", "contacts"),
    "hot_leads": ("graph", "hot_leads"),
    "channels": ("growth", "channels"),
    "bots": ("bots", "total"),
    "community_nodes": ("bots", "community_nodes"),
    "failed_24h": ("ops", "failed_24h"),
    "retained": ("retention", "retained"),
    "seo_weak": ("seo", "weak"),
    "waiting_reply": ("vault", "waiting_reply"),
}

# Для этих метрик РОСТ — это плохо (тренд «вверх» подсвечиваем как негатив).
_LOWER_IS_BETTER = {"fleet_dead", "failed_24h", "seo_weak", "waiting_reply"}

_DIGEST_STATE_KEY = "digest_metrics"


def _dig(snap: dict, block: str, key: str, default=0):
    b = snap.get(block) or {}
    v = b.get(key, default)
    return v if v is not None else default


def _flat_metrics(snap: dict) -> dict:
    """Плоский снимок ключевых метрик — для хранения и сравнения трендов."""
    return {name: int(_dig(snap, blk, key, 0) or 0)
            for name, (blk, key) in _TREND_METRICS.items()}


def _trend(name: str, cur: int, prev) -> dict | None:
    """Дельта метрики к прошлому дайджесту. None если прошлого нет."""
    if prev is None or name not in prev:
        return None
    try:
        delta = int(cur) - int(prev[name])
    except (TypeError, ValueError):
        return None
    if delta == 0:
        return {"delta": 0, "dir": "flat", "good": None}
    up = delta > 0
    good = (not up) if name in _LOWER_IS_BETTER else up
    return {"delta": delta, "dir": "up" if up else "down", "good": bool(good)}


def _headline(snap: dict) -> str:
    """Одна строка статуса проекта — по губернатору, банам и застою роста."""
    fleet = snap.get("fleet") or {}
    growth = snap.get("growth") or {}
    level = fleet.get("governor_level", "green")
    bans = int(fleet.get("bans_24h", 0) or 0)
    if int(fleet.get("accounts", 0) or 0) == 0:
        return "🚀 Пустой проект — начните с добавления аккаунтов и первого канала."
    if bans > 0 or level == "red":
        return "🔴 Флот под давлением — снизьте темп и дайте аккаунтам прогреться."
    if level == "orange":
        return "🟠 Умеренный риск — темп умеренный, следите за флудом."
    if int(growth.get("channels", 0) or 0) > 0 and int(growth.get("growth_ops_7d", 0) or 0) == 0:
        return "🟡 Флот здоров, но рост стоит — запустите продвижение."
    return "🟢 Всё здорово — флот в норме, можно масштабировать."


def compose_digest(snap: dict, suggestions=None, narrative_text: str = "",
                   prev_metrics: dict | None = None,
                   now: datetime | None = None) -> dict:
    """Собрать структуру дайджеста из снимка мира + подсказок мозга + трендов.

    Чистая функция: без БД/сети. suggestions — список карточек brain
    (kind/title/why/action), prev_metrics — плоский снимок прошлого раза.
    """
    now = now or datetime.now(timezone.utc)
    suggestions = list(suggestions or [])

    def stat(label, block, key, metric_name=None, suffix=""):
        val = int(_dig(snap, block, key, 0) or 0)
        s = {"label": label, "value": val, "suffix": suffix}
        if metric_name:
            t = _trend(metric_name, val, prev_metrics)
            if t:
                s["trend"] = t
        return s

    fleet = snap.get("fleet") or {}
    sections = [
        {"key": "fleet", "title": "🛰 Флот", "stats": [
            stat("Активных аккаунтов", "fleet", "active", "fleet_active"),
            stat("Мёртвых", "fleet", "dead", "fleet_dead"),
            {"label": "Давление флота", "value": int(fleet.get("pressure", 0) or 0),
             "suffix": f"/100 · ×{fleet.get('governor_mult', 1.0)}"},
        ], "note": f"Губернатор: {fleet.get('governor_level', 'green')}, банов за 24ч: {int(fleet.get('bans_24h', 0) or 0)}"},
        {"key": "audience", "title": "👥 Аудитория", "stats": [
            stat("Контактов", "graph", "contacts", "contacts"),
            stat("Горячих лидов", "graph", "hot_leads", "hot_leads"),
            stat("Намерений за 24ч", "graph", "intents_24h"),
        ], "note": ""},
        {"key": "growth", "title": "📈 Рост", "stats": [
            stat("Каналов", "growth", "channels", "channels"),
            stat("Операций роста (7д)", "growth", "growth_ops_7d"),
        ], "note": ""},
        {"key": "network", "title": "🕸 Сеть", "stats": [
            stat("Ботов", "bots", "total", "bots"),
            stat("Комьюнити-нод", "bots", "community_nodes", "community_nodes"),
            stat("Пустых нод", "bots", "community_empty"),
        ], "note": ""},
        {"key": "risks", "title": "⚠️ Операции и риски", "stats": [
            stat("Ошибок за 24ч", "ops", "failed_24h", "failed_24h"),
            stat("В очереди", "ops", "pending"),
            stat("Выполняется", "ops", "running"),
        ], "note": (lambda lf: f"Последний сбой: {lf['op_type']} — {lf['reason']}" if lf else "")(
            (snap.get("ops") or {}).get("last_failed"))},
    ]

    # Рекомендации — из мозга (топ по severity), максимум 5. Порядок важности
    # совпадает со значениями brain: urgent → warn → opportunity → info.
    _sev = {"urgent": 0, "warn": 1, "opportunity": 2, "info": 3}
    recs = sorted(suggestions, key=lambda s: _sev.get(s.get("severity", "info"), 4))[:5]
    recommendations = [{
        "title": s.get("title", ""),
        "why": s.get("why", ""),
        "action": s.get("action", {}),
        "severity": s.get("severity", "info"),
    } for s in recs]

    return {
        "generated_at": now.isoformat(),
        "headline": _headline(snap),
        "narrative": narrative_text or "",
        "sections": sections,
        "recommendations": recommendations,
        "has_prev": prev_metrics is not None,
    }


async def build(pool, owner_id: int) -> dict:
    """Собрать дайджест из живого организма и обновить базу трендов.

    Fail-open поблочно: любой сбой источника не роняет отчёт целиком.
    """
    from services.organism import world, brain, spine
    snap = await world.snapshot(pool, owner_id)
    try:
        suggestions = brain.build_suggestions(snap)
    except Exception:
        suggestions = []
    try:
        narrative_text = brain.narrative(snap)
    except Exception:
        narrative_text = ""
    prev = None
    try:
        prev = await spine.state_get(pool, owner_id, _DIGEST_STATE_KEY, None)
    except Exception:
        prev = None
    digest = compose_digest(snap, suggestions, narrative_text, prev)
    # Сохраняем текущие метрики как базу для трендов следующего дайджеста.
    try:
        await spine.state_set(pool, owner_id, _DIGEST_STATE_KEY, _flat_metrics(snap))
    except Exception:
        pass
    return digest

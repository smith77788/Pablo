"""Мир-снимок организма: ОДИН живой контекст из всех подсистем.

Раньше каждый модуль собирал своё состояние сам (next_actions._gather_state,
ecosystem_copilot._analyze_owner, …) — отсюда «россыпь островов». Здесь единый
snapshot композит: флот+губернатор, операции, граф контактов/CRM, хранилище,
активная цель, счётчики событий за сутки. Каждый блок fail-open — сбой одной
подсистемы не рушит картину.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

HOT_TAGS = ["горячий", "интерес-цена"]
HOT_STAGES = ("proposal", "negotiation")


async def snapshot(pool, owner_id: int) -> dict:
    return {
        "fleet": await _fleet(pool, owner_id),
        "ops": await _ops(pool, owner_id),
        "graph": await _graph(pool, owner_id),
        "vault": await _vault(pool, owner_id),
        "goal": await _goal(pool, owner_id),
        "growth": await _growth(pool, owner_id),
        "bots": await _bots(pool, owner_id),
        "events_24h": await _events(pool, owner_id),
    }


async def _bots(pool, owner_id: int) -> dict:
    """Сеть управляемых ботов: всего/активны/неактивны — для заметки мозга."""
    out = {"total": 0, "active": 0, "inactive": 0}
    try:
        r = await pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE is_active) AS active "
            "FROM managed_bots WHERE added_by=$1", owner_id)
        if r:
            out["total"] = int(r["total"] or 0)
            out["active"] = int(r["active"] or 0)
            out["inactive"] = out["total"] - out["active"]
    except Exception:
        log.debug("world._bots failed owner=%s", owner_id)
    return out


async def _growth(pool, owner_id: int) -> dict:
    """Активность роста и наличие каналов — для подсказки «застой роста»."""
    out = {"channels": 0, "growth_ops_7d": 0}
    try:
        from services import growth_center
        out["channels"] = int(await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", owner_id) or 0)
        out["growth_ops_7d"] = int(await pool.fetchval(
            "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 "
            "AND op_type = ANY($2::text[]) AND created_at > NOW() - interval '7 days'",
            owner_id, list(growth_center.GROWTH_OP_TYPES)) or 0)
    except Exception:
        log.debug("world._growth failed owner=%s", owner_id)
    return out


async def _fleet(pool, owner_id: int) -> dict:
    out = {"accounts": 0, "active": 0, "dead": 0, "bans_24h": 0,
           "pressure": 0, "governor_mult": 1.0, "governor_level": "green"}
    try:
        r = await pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE is_active AND COALESCE(acc_status,'ok') "
            "  NOT IN ('banned','spamblock','deactivated','session_expired')) AS active, "
            "COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok') "
            "  IN ('banned','deactivated','session_expired')) AS dead "
            "FROM tg_accounts WHERE owner_id=$1", owner_id)
        if r:
            out["accounts"], out["active"], out["dead"] = int(r["total"]), int(r["active"]), int(r["dead"])
    except Exception:
        log.debug("world._fleet failed owner=%s", owner_id)
    try:
        from services import fleet_governor
        g = await fleet_governor.status(pool, owner_id)
        out["pressure"] = g.get("score", 0)
        out["governor_mult"] = g.get("multiplier", 1.0)
        out["governor_level"] = g.get("level", "green")
    except Exception:
        log.debug("world._fleet governor failed owner=%s", owner_id)
    try:
        from services.organism import spine
        out["bans_24h"] = int((await spine.event_counts(pool, owner_id, hours=24)).get("ban", 0))
    except Exception:
        pass
    try:
        from services import geo_router
        dist = await geo_router.get_geo_distribution(pool, owner_id)
        out["geo"] = geo_router.summarize_distribution(dist)
    except Exception:
        log.debug("world._fleet geo failed owner=%s", owner_id)
    return out


async def _ops(pool, owner_id: int) -> dict:
    out = {"running": 0, "pending": 0, "failed_24h": 0, "last_failed": None}
    try:
        r = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status='running') AS running, "
            "COUNT(*) FILTER (WHERE status='pending') AS pending, "
            "COUNT(*) FILTER (WHERE status='failed' AND finished_at > now()-interval '24 hours') AS failed "
            "FROM operation_queue WHERE owner_id=$1", owner_id)
        if r:
            out["running"], out["pending"], out["failed_24h"] = int(r["running"]), int(r["pending"]), int(r["failed"])
        lf = await pool.fetchrow(
            "SELECT id, op_type, COALESCE(error_msg,'') AS err FROM operation_queue "
            "WHERE owner_id=$1 AND status='failed' ORDER BY finished_at DESC LIMIT 1", owner_id)
        if lf:
            out["last_failed"] = {"op_id": int(lf["id"]), "op_type": lf["op_type"],
                                  "reason": (lf["err"] or "")[:200]}
    except Exception:
        log.debug("world._ops failed owner=%s", owner_id)
    return out


async def _graph(pool, owner_id: int) -> dict:
    out = {"contacts": 0, "hot_leads": 0, "intents_24h": 0}
    try:
        out["contacts"] = int(await pool.fetchval(
            "SELECT COUNT(*) FROM unified_contacts WHERE owner_id=$1", owner_id) or 0)
        out["hot_leads"] = int(await pool.fetchval(
            "SELECT COUNT(DISTINCT uc.id) FROM unified_contacts uc "
            "LEFT JOIN contact_crm cc ON cc.contact_id=uc.id "
            "WHERE uc.owner_id=$1 AND (cc.stage = ANY($2::text[]) OR uc.tags && $3::text[])",
            owner_id, list(HOT_STAGES), HOT_TAGS) or 0)
    except Exception:
        log.debug("world._graph failed owner=%s", owner_id)
    try:
        from services.organism import spine
        counts = await spine.event_counts(pool, owner_id, hours=24)
        out["intents_24h"] = int(counts.get("intent", 0))
    except Exception:
        pass
    return out


async def _vault(pool, owner_id: int) -> dict:
    try:
        from services import vault_service
        d = await vault_service.diagnostics(pool, owner_id)
        return {"health": d.get("health"), "stale_days": d.get("stale_days")}
    except Exception:
        return {"health": None, "stale_days": None}


async def _goal(pool, owner_id: int):
    try:
        from services.organism import spine
        return await spine.state_get(pool, owner_id, "goal", None)
    except Exception:
        return None


async def _events(pool, owner_id: int) -> dict:
    try:
        from services.organism import spine
        return await spine.event_counts(pool, owner_id, hours=24)
    except Exception:
        return {}

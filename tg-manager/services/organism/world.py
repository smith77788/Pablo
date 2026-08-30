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
        "seo": await _seo(pool, owner_id),
        "retention": await _retention(pool, owner_id),
        "anomalies": await _anomalies(pool, owner_id),
        "invite_chats": await _invite_chats(pool, owner_id),
        "bots": await _bots(pool, owner_id),
        "chat_warmup": await _chat_warmup(pool, owner_id),
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
    # Ноды-комьюнити: всего и «пустых» (без каналов) — для подсказки «наполните».
    try:
        r2 = await pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE (SELECT COUNT(*) FROM community_channels c WHERE c.node_id=n.id)=0) AS empty "
            "FROM community_nodes n WHERE n.owner_id=$1 AND n.is_active", owner_id)
        if r2:
            out["community_nodes"] = int(r2["total"] or 0)
            out["community_empty"] = int(r2["empty"] or 0)
    except Exception:
        log.debug("world._bots community failed owner=%s", owner_id)
    return out


async def _chat_warmup(pool, owner_id: int) -> dict:
    """Разогрев чатов флотом: активные/простаивающие сессии + сколько групп-чатов
    у владельца — для подсказки «чаты тихие, оживите флотом»."""
    out = {"active": 0, "stalled": 0, "chats": 0}
    try:
        r = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status='active') AS active, "
            "COUNT(*) FILTER (WHERE status='active' AND (last_run_at IS NULL "
            "  OR last_run_at < NOW() - INTERVAL '30 minutes')) AS stalled "
            "FROM chat_warmup_sessions WHERE owner_id=$1", owner_id)
        if r:
            out["active"] = int(r["active"] or 0)
            out["stalled"] = int(r["stalled"] or 0)
    except Exception:
        log.debug("world._chat_warmup failed owner=%s", owner_id)
    try:
        out["chats"] = int(await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1 "
            "AND type IN ('megagroup','supergroup','group','chat')", owner_id) or 0)
    except Exception:
        log.debug("world._chat_warmup chats failed owner=%s", owner_id)
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


async def _seo(pool, owner_id: int) -> dict:
    """Находимость своих каналов в поиске Telegram: сколько объектов слабо
    оптимизированы (grade 'red' по seo_advisor) — чистая эвристика по
    заголовку/@username/описанию, без сети. Для подсказки мозга «оптимизируйте SEO».
    """
    out = {"scored": 0, "weak": 0, "worst": None}
    try:
        from services import seo_advisor
        rows = await pool.fetch(
            "SELECT title, username, about FROM managed_channels "
            "WHERE owner_id=$1 LIMIT 200", owner_id)
        worst_score = 101
        for r in (rows or []):
            res = seo_advisor.analyze(
                title=r["title"] or "", username=r["username"] or "",
                description=r["about"] or "")
            out["scored"] += 1
            if res["grade"] == "red":
                out["weak"] += 1
            if res["score"] < worst_score:
                worst_score = res["score"]
                name = (r["title"] or "").strip() \
                    or (("@" + r["username"]) if r["username"] else "Канал")
                out["worst"] = name
    except Exception:
        log.debug("world._seo failed owner=%s", owner_id)
    return out


async def _retention(pool, owner_id: int) -> dict:
    """Ретеншен инвайта за 30 дн.: приток (успешные вступления по инвайт-операциям)
    vs отток (события 'left' из chat_guard). Переиспускает те же выборки, что и
    эндпоинт, + чистую свёртку invite_retention. Для подсказки мозга «отток —
    welcome не удерживает». fail-open."""
    out = {"joined": 0, "left": 0, "retained": None,
           "retention_pct": None, "churn_pct": None, "health": "unknown"}
    try:
        from services import invite_retention
        joined = int(await pool.fetchval(
            """SELECT COUNT(*) FROM operation_log ol
               JOIN operation_queue oq ON oq.id = ol.op_id
               WHERE oq.owner_id=$1 AND oq.op_type LIKE '%invite%'
                 AND ol.status='ok' AND ol.message='joined'
                 AND ol.created_at > NOW() - INTERVAL '30 days'""", owner_id) or 0)
        left = int(await pool.fetchval(
            "SELECT COUNT(*) FROM organism_events WHERE owner_id=$1 AND kind='left' "
            "AND created_at > NOW() - INTERVAL '30 days'", owner_id) or 0)
        s = invite_retention.summarize(joined, left)
        s["health"] = invite_retention.health(s["retention_pct"])
        out = s
    except Exception:
        log.debug("world._retention failed owner=%s", owner_id)
    return out


async def _anomalies(pool, owner_id: int) -> dict:
    """Активные аномалии за 24ч (детектор аномалий): critical/warning + верхняя.
    Прямой сигнал риска для флота — для срочной подсказки мозга. fail-open."""
    out = {"critical": 0, "warning": 0, "top": None}
    try:
        r = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE severity='critical') AS crit, "
            "COUNT(*) FILTER (WHERE severity='warning') AS warn "
            "FROM anomaly_events WHERE owner_id=$1 AND is_active=TRUE "
            "AND detected_at > NOW() - INTERVAL '24 hours'", owner_id)
        if r:
            out["critical"] = int(r["crit"] or 0)
            out["warning"] = int(r["warn"] or 0)
        if out["critical"] or out["warning"]:
            t = await pool.fetchval(
                "SELECT title FROM anomaly_events WHERE owner_id=$1 AND is_active=TRUE "
                "AND detected_at > NOW() - INTERVAL '24 hours' "
                "ORDER BY (severity='critical') DESC, detected_at DESC LIMIT 1", owner_id)
            out["top"] = t
    except Exception:
        log.debug("world._anomalies failed owner=%s", owner_id)
    return out


async def _invite_chats(pool, owner_id: int) -> dict:
    """Безопасный инвайтинг (governor уровня чата): сколько чатов сейчас на паузе
    приёма (chat-flood/негатив) и сколько «мёртвых» по живости. Сигнал: приём в
    эти чаты флудит — лить нельзя. fail-open."""
    out = {"frozen": 0, "dead": 0}
    try:
        r = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE paused_until IS NOT NULL AND paused_until > NOW()) AS frozen, "
            "COUNT(*) FILTER (WHERE liveness_score IS NOT NULL AND liveness_score <= 0.05) AS dead "
            "FROM chat_invite_state WHERE owner_id=$1", owner_id)
        if r:
            out["frozen"] = int(r["frozen"] or 0)
            out["dead"] = int(r["dead"] or 0)
    except Exception:
        log.debug("world._invite_chats failed owner=%s", owner_id)
    return out


async def _fleet(pool, owner_id: int) -> dict:
    out = {"accounts": 0, "active": 0, "dead": 0, "restricted": 0, "bans_24h": 0,
           "pressure": 0, "governor_mult": 1.0, "governor_level": "green"}
    try:
        r = await pool.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE is_active AND COALESCE(acc_status,'ok') "
            "  NOT IN ('banned','spamblock','deactivated','session_expired')) AS active, "
            "COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok') "
            "  IN ('banned','deactivated','session_expired')) AS dead, "
            "COUNT(*) FILTER (WHERE COALESCE(acc_status,'ok') = 'spamblock') AS restricted "
            "FROM tg_accounts WHERE owner_id=$1", owner_id)
        if r:
            out["accounts"], out["active"], out["dead"] = int(r["total"]), int(r["active"]), int(r["dead"])
            out["restricted"] = int(r["restricted"] or 0)
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
    out = {"health": None, "stale_days": None, "waiting_reply": 0}
    try:
        from services import vault_service
        d = await vault_service.diagnostics(pool, owner_id)
        out["health"] = d.get("health")
        out["stale_days"] = d.get("stale_days")
    except Exception:
        return out
    # Диалоги, где последнее сообщение — входящее (клиент ждёт ответа) за 7 дн.
    # Один дешёвый агрегат; сильный сигнал для продаж. fail-open.
    try:
        n = await pool.fetchval(
            "SELECT COUNT(*) FROM ("
            "  SELECT chat_id, "
            "    MAX(msg_date) FILTER (WHERE direction='in')  AS last_in, "
            "    MAX(msg_date) FILTER (WHERE direction='out') AS last_out "
            "  FROM vault_messages WHERE owner_id=$1 "
            "    AND msg_date > NOW() - INTERVAL '7 days' GROUP BY chat_id"
            ") t WHERE last_in IS NOT NULL AND (last_out IS NULL OR last_in > last_out)",
            owner_id)
        out["waiting_reply"] = int(n or 0)
    except Exception:
        log.debug("world._vault waiting failed owner=%s", owner_id)
    return out


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

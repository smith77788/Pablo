"""Инфраструктура — маршруты мини-аппа: сводка, пулы аккаунтов, журнал аудита.

Сводка `/api/miniapp/infra` перенесена сюда из `services/mini_app_api.py`
вместе с новыми ручками модуля: всё, что открывает вкладка «Пулы и аудит»,
лежит в одном месте. Логика — в `services/infra_hub.py`.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты модуля «Инфраструктура»."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _list_limit, _list_offset,
        log,
    )
    from services import infra_hub as _ih

    async def _record(uid: int, action: str, target: str) -> None:
        try:
            from database.db import record_manual_action
            await record_manual_action(pool, uid, action, target=target[:200])
        except Exception:
            log.debug("infra: журнал действия %s не записан", action, exc_info=True)

    async def _infra_body(request: web.Request) -> dict:
        try:
            b = await request.json()
        except Exception:
            return {}
        return b if isinstance(b, dict) else {}

    async def infra_analytics_overview(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)

        async def _val(sql: str, what: str) -> int:
            try:
                return int(await pool.fetchval(sql, uid) or 0)
            except Exception as e:
                log.warning("infra_analytics %s uid=%d: %s", what, uid, e)
                return 0

        active_accs = await _val(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            "active_accs")
        flood_24h = await _val(
            """SELECT COUNT(*) FROM account_flood_log fl
               JOIN tg_accounts ta ON ta.id=fl.account_id
               WHERE ta.owner_id=$1 AND fl.created_at > NOW()-INTERVAL '24h'""",
            "flood_24h")
        ops_24h = await _val(
            "SELECT COUNT(*) FROM operation_queue WHERE owner_id=$1 "
            "AND created_at > NOW()-INTERVAL '24h'", "ops_24h")
        warmup_active = await _val(
            "SELECT COUNT(*) FROM account_warmup_plans wp "
            "WHERE wp.owner_id=$1 AND wp.status='active'", "warmup")
        fails_24h = await _val(
            "SELECT COUNT(*) FROM operation_audit WHERE owner_id=$1 "
            "AND occurred_at > NOW()-INTERVAL '24h' "
            "AND NOT (result = ANY(ARRAY['success','ok','done','requested',"
            "'partial','flood_wait','skipped']))", "fails_24h")
        try:
            pools = await _ih.pools_overview(pool, uid)
        except Exception as e:
            log.warning("infra_analytics pools uid=%d: %s", uid, e)
            pools = {"pools": [], "unpooled": {"total": 0, "alive": 0}, "system": []}
        try:
            audit = (await _ih.audit_page(pool, uid, period="all", limit=10))["rows"]
        except Exception as e:
            log.warning("infra_analytics audit uid=%d: %s", uid, e)
            audit = []
        return _json_resp({
            "active_accounts": active_accs,
            "flood_24h": flood_24h,
            "ops_24h": ops_24h,
            "warmup_active": warmup_active,
            "fails_24h": fails_24h,
            # Старый формат списка пулов (pool, cnt) оставлен для бота и
            # чужих экранов; полная сводка — в pools_full.
            "pools": [{"pool": p["pool"], "cnt": p["total"]} for p in pools["pools"]],
            "pools_full": pools["pools"],
            "unpooled": pools["unpooled"],
            "system_pools": pools["system"],
            "audit": audit,
        })

    # ── Пулы ──────────────────────────────────────────────────────────────

    async def infra_pools(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            return _json_resp(await _ih.pools_overview(pool, uid))
        except Exception:
            log.exception("infra_pools uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def infra_pools_assign(request: web.Request) -> web.Response:
        """Назначить пул. body: {pool, account_ids?: [...], mode?: 'ids'|'unpooled'|'all'}.

        'unpooled' — все аккаунты без пула («собрать новых в пул»),
        'ids' — выбранные. Пул '' или null — снять пул с выбранных.
        """
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        body = await _infra_body(request)
        raw = body.get("pool")
        if raw in (None, ""):
            name = None
        else:
            try:
                name = _ih.normalize_pool_name(raw)
            except _ih.PoolNameError as e:
                return _err(str(e), 400)
        mode = str(body.get("mode") or "ids")
        if mode not in ("ids", "unpooled", "all"):
            return _err("Неизвестный режим назначения", 400)
        ids = None
        if mode == "ids":
            ids = [int(x) for x in (body.get("account_ids") or [])
                   if str(x).lstrip("-").isdigit()]
            if not ids:
                return _err("Выберите аккаунты", 400)
        try:
            n = await _ih.assign_pool(pool, uid, name, account_ids=ids,
                                      only_unpooled=(mode == "unpooled"))
        except Exception:
            log.exception("infra_pools_assign uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if n:
            await _record(uid, "pool_assign", f"{_ih.pool_label(name)}: {n}")
        return _json_resp({"ok": True, "count": n, "pool": name,
                           "label": _ih.pool_label(name)})

    async def infra_pools_rename(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        body = await _infra_body(request)
        old = str(body.get("from") or "").strip()
        if not old:
            return _err("Не указан пул", 400)
        try:
            new = _ih.normalize_pool_name(body.get("to"))
        except _ih.PoolNameError as e:
            return _err(str(e), 400)
        try:
            n = await _ih.rename_pool(pool, uid, old, new)
        except Exception:
            log.exception("infra_pools_rename uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if n:
            await _record(uid, "pool_rename",
                          f"{_ih.pool_label(old)} → {_ih.pool_label(new)}: {n}")
        return _json_resp({"ok": True, "count": n, "pool": new,
                           "label": _ih.pool_label(new)})

    async def infra_pools_dissolve(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        body = await _infra_body(request)
        name = str(body.get("pool") or "").strip()
        if not name:
            return _err("Не указан пул", 400)
        try:
            n = await _ih.dissolve_pool(pool, uid, name)
        except Exception:
            log.exception("infra_pools_dissolve uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if n:
            await _record(uid, "pool_dissolve", f"{_ih.pool_label(name)}: {n}")
        return _json_resp({"ok": True, "count": n})

    async def infra_pools_rebalance(request: web.Request) -> web.Response:
        """GET — что изменит автораспределение; POST — применить."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            if request.method == "POST":
                res = await _ih.rebalance_apply(pool, uid)
                if res["changed"]:
                    await _record(uid, "pool_rebalance", f"{res['changed']}")
                return _json_resp({"ok": True, **res})
            plan = await _ih.rebalance_plan(pool, uid)
            return _json_resp({"changes": plan[:200], "total": len(plan)})
        except Exception:
            log.exception("infra_pools_rebalance uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    # ── Журнал аудита ─────────────────────────────────────────────────────

    async def infra_audit(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        qs = request.rel_url.query
        result = qs.get("result") or "all"
        if result not in _ih.AUDIT_RESULT_FILTERS:
            result = "all"
        period = qs.get("period") or "7d"
        if period not in _ih.AUDIT_PERIODS:
            period = "7d"
        action = (qs.get("action") or "").strip()[:64]
        acc = qs.get("account_id") or ""
        account_id = int(acc) if acc.isdigit() else None
        pool_name = (qs.get("pool") or "").strip()[:64]
        q = (qs.get("q") or "").strip()[:64]
        try:
            page = await _ih.audit_page(
                pool, uid, result=result, period=period, action=action,
                account_id=account_id, pool_name=pool_name, q=q,
                offset=_list_offset(request), limit=_list_limit(request, 30, 200))
            if qs.get("summary") == "1":
                page["summary"] = await _ih.audit_summary(pool, uid, period)
            # Итог журнала — явным ключом: листание и подпись «показаны N из M»
            # держатся на нём (сторож tests/test_a_capped_list_says_it_is_capped.py).
            return _json_resp({**page, "total": int(page.get("total") or 0)})
        except Exception:
            log.exception("infra_audit uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/infra", infra_analytics_overview)
    app.router.add_get("/api/miniapp/infra/pools", infra_pools)
    app.router.add_post("/api/miniapp/infra/pools/assign", infra_pools_assign)
    app.router.add_post("/api/miniapp/infra/pools/rename", infra_pools_rename)
    app.router.add_post("/api/miniapp/infra/pools/dissolve", infra_pools_dissolve)
    app.router.add_get("/api/miniapp/infra/pools/rebalance", infra_pools_rebalance)
    app.router.add_post("/api/miniapp/infra/pools/rebalance", infra_pools_rebalance)
    app.router.add_get("/api/miniapp/infra/audit", infra_audit)

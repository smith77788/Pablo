"""Ручка аналитики продаж: GET /api/miniapp/crm/sales_insights?days=30|90|180|365.

Расчёт — services/crm_sales_insights; здесь только граница HTTP (владелец,
окно отчёта, ошибки по-русски)."""
from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


def setup_routes(app: web.Application, pool) -> None:
    from services.crm_sales_insights import DEFAULT_WINDOW, WINDOWS, report
    from services.mini_app_api import _INTERNAL_ERROR, _err, _get_uid, _json_resp

    async def crm_sales_insights(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        raw = (request.query.get("days") or "").strip()
        days = int(raw) if raw.isdigit() else DEFAULT_WINDOW
        if days not in WINDOWS:
            return _err("Окно отчёта — 30, 90, 180 или 365 дней", 400)
        try:
            return _json_resp(await report(pool, uid, days))
        except Exception:
            log.exception("crm_sales_insights uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/crm/sales_insights", crm_sales_insights)

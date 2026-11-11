"""«🔭 Радар ниши» в мини-аппе: каталог чужих ресурсов и присутствие в них.

Логика — services/niche_radar.py, работа — операции `niche_scan` и
`niche_presence` в op_worker. Здесь только двери: кампания, запуск поиска,
список найденного, запуск присутствия и журнал действий. Чужая кампания —
«не найдено».
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    from services import niche_radar as nr
    from services import operation_bus as _obus
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, bad_body_response, log,
    )
    from services.security import validate_integer

    async def _niche_body(request):
        try:
            data = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return None, (bad if bad is not None else _err("Не удалось разобрать запрос", 400))
        if not isinstance(data, dict):
            return None, _err("Не удалось разобрать запрос", 400)
        return data, None

    async def _own(uid: int, cid) -> dict | None:
        return await nr.get_campaign(pool, uid, cid)

    async def niche_overview(request: web.Request) -> web.Response:
        """Кампании ниши с итогами: сколько нашли, вступили, что сделали."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rows = await pool.fetch(
                "SELECT * FROM niche_campaigns WHERE owner_id=$1 AND status <> 'archived' "
                "ORDER BY created_at DESC LIMIT 50", uid)
            items = []
            for r in rows:
                d = dict(r)
                d["stats"] = await nr.campaign_stats(pool, uid, int(d["id"]))
                d["tone_ru"] = nr.TONE_RU.get(d["tone"], d["tone"])
                items.append(d)
            accounts = int(await pool.fetchval(
                "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active "
                "AND session_str IS NOT NULL", uid) or 0)
        except Exception:
            log.exception("niche_overview uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"campaigns": items, "accounts": accounts,
                           "tones": [{"key": k, "label": v} for k, v in nr.TONE_RU.items()]})

    async def niche_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _niche_body(request)
        if bad is not None:
            return bad
        try:
            c = nr.clean_campaign(data)
            cid = await nr.save_campaign(pool, uid, c, data.get("id") or None)
        except nr.NicheError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("niche_save uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "id": cid})

    async def niche_keywords(request: web.Request) -> web.Response:
        """Какие запросы уйдут в поиск — показать ДО запуска."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _niche_body(request)
        if bad is not None:
            return bad
        try:
            c = nr.clean_campaign(data)
        except nr.NicheError as exc:
            return _err(str(exc), 400)
        ai = await nr.ai_keywords(c["niche"], c["geo"]) if data.get("ai") else []
        words = nr.expand_keywords(c["niche"], c["geo"], c["keywords"] + ai)
        return _json_resp({"keywords": words, "ai": ai})

    async def _submit(uid: int, op_type: str, params: dict, label: str, total: int):
        return await _obus.submit(pool, uid, op_type, params, total_items=total, label=label)

    async def niche_scan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        c = await _own(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        try:
            op_id = await _submit(uid, "niche_scan", {"campaign_id": int(c["id"])},
                                  f"Радар ниши: {c['title']}"[:120], 1)
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("niche_scan uid=%s cid=%s", uid, c["id"])
            return _err("Не удалось поставить поиск в очередь", 500)
        return _json_resp({"ok": True, "op_id": op_id})

    async def niche_presence(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _niche_body(request)
        if bad is not None:
            return bad
        c = await _own(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        actions = [a for a in (data.get("actions") or []) if a in ("join", "comment", "reaction")]
        if not actions:
            return _err("Выберите хотя бы одно действие", 400)
        ready = int(await pool.fetchval(
            "SELECT COUNT(*) FROM niche_assets WHERE owner_id=$1 AND campaign_id=$2 "
            "AND status IN ('found','joined')", uid, int(c["id"])) or 0)
        if not ready:
            return _err("В каталоге нет ресурсов — сначала запустите поиск", 400)
        acc_count = validate_integer(data.get("acc_count"), min_val=1, max_val=10) or 3
        limit = validate_integer(data.get("limit"), min_val=1, max_val=60) or 12
        params = {"campaign_id": int(c["id"]), "actions": actions,
                  "acc_count": int(acc_count), "limit": int(limit)}
        try:
            op_id = await _submit(uid, "niche_presence", params,
                                  f"Присутствие в нише: {c['title']}"[:120], int(limit))
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("niche_presence uid=%s cid=%s", uid, c["id"])
            return _err("Не удалось поставить присутствие в очередь", 500)
        return _json_resp({"ok": True, "op_id": op_id, "targets": min(ready, int(limit))})

    async def niche_assets(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        c = await _own(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        q = request.rel_url.query
        limit = validate_integer(q.get("limit", "50"), min_val=1, max_val=100) or 50
        offset = validate_integer(q.get("offset", "0"), min_val=0) or 0
        status = str(q.get("status") or "")
        try:
            data = await nr.list_assets(pool, uid, int(c["id"]), status=status,
                                        limit=int(limit), offset=int(offset))
        except Exception:
            log.exception("niche_assets uid=%s cid=%s", uid, c["id"])
            return _err(_INTERNAL_ERROR, 500)
        data["stats"] = await nr.campaign_stats(pool, uid, int(c["id"]))
        return _json_resp(data)

    async def niche_log(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        c = await _own(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        rows = await pool.fetch(
            """SELECT a.kind, a.status, a.detail, a.created_at, n.title, n.username
                 FROM niche_asset_actions a JOIN niche_assets n ON n.id = a.asset_id
                WHERE a.owner_id=$1 AND n.campaign_id=$2
                ORDER BY a.created_at DESC LIMIT 60""", uid, int(c["id"]))
        return _json_resp({"items": [dict(r) for r in rows]})

    async def niche_asset_skip(request: web.Request) -> web.Response:
        """Убрать ресурс из работы: не наша тема, не хотим туда."""
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            aid = int(request.match_info["aid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        res = await pool.execute(
            "UPDATE niche_assets SET status='skipped', skip_reason='убрано вручную' "
            "WHERE id=$1 AND owner_id=$2", aid, uid)
        if res == "UPDATE 0":
            return _err("Ресурс не найден", 404)
        return _json_resp({"ok": True})

    async def niche_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        c = await _own(uid, request.match_info.get("cid"))
        if not c:
            return _err("Кампания не найдена", 404)
        await pool.execute("DELETE FROM niche_campaigns WHERE id=$1 AND owner_id=$2",
                           int(c["id"]), uid)
        return _json_resp({"ok": True})

    app.router.add_get("/api/miniapp/niche/overview", niche_overview)
    app.router.add_post("/api/miniapp/niche/campaign", niche_save)
    app.router.add_post("/api/miniapp/niche/keywords", niche_keywords)
    app.router.add_get("/api/miniapp/niche/campaign/{cid}/assets", niche_assets)
    app.router.add_get("/api/miniapp/niche/campaign/{cid}/log", niche_log)
    app.router.add_post("/api/miniapp/niche/campaign/{cid}/scan", niche_scan)
    app.router.add_post("/api/miniapp/niche/campaign/{cid}/presence", niche_presence)
    app.router.add_post("/api/miniapp/niche/campaign/{cid}/delete", niche_delete)
    app.router.add_post("/api/miniapp/niche/asset/{aid}/skip", niche_asset_skip)

"""Экран «Интеграции» мини-аппа: API-ключи, исходящие вебхуки, Google Sheets.

Логика — в `services/api_keys.py`, `services/webhooks.py`,
`services/google_sheets.py`; здесь только границы запроса: кто владелец, что
пришло в теле, какой текст отказа увидит экран. Каждый вызов сервиса получает
owner_id — чужой id получателя или доставки даёт «не найдено».

Секрет подписи вебхука отдаётся в поле `signing_secret` (а не `secret`):
`secret` по имени вычищает последний барьер ответа (`secret_masking`), а этот
секрет владельцу показать нужно — ровно один раз, при создании и перевыпуске.
"""
from __future__ import annotations

from aiohttp import web


def _api_base(request: web.Request) -> str:
    proto = request.headers.get("X-Forwarded-Proto", request.scheme or "https")
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}/api/v1"


_DELIVERY_PAGE = 100


def setup_routes(app: web.Application, pool) -> None:
    from services import api_keys as _keys
    from services import google_sheets as _gs
    from services import webhooks as _wh
    from services.mini_app_api import _INTERNAL_ERROR, _err, _get_uid, _json_resp, log

    async def _body(request) -> dict:
        try:
            d = await request.json()
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}

    def _int_id(request, name: str) -> int | None:
        try:
            v = int(request.match_info[name])
        except (KeyError, ValueError):
            return None
        return v if v > 0 else None

    def _str(v, limit: int = 2000) -> str:
        return v.strip()[:limit] if isinstance(v, str) else ""

    def _key_public(k: dict) -> dict:
        return {
            "id": k["id"], "name": k.get("name") or "",
            "prefix": k.get("key_prefix") or "",
            "is_active": bool(k.get("is_active")),
            "scopes": list(k.get("scopes") or []),
            "requests_total": int(k.get("requests_total") or 0),
            "created_at": k["created_at"].isoformat() if k.get("created_at") else None,
            "last_used_at": k["last_used_at"].isoformat() if k.get("last_used_at") else None,
        }

    async def integrations_overview(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            keys = await _keys.list_keys(pool, uid)
            endpoints = await _wh.list_endpoints(pool, uid)
            sheets = await _gs.get_connection(pool, uid)
        except Exception:
            log.exception("integrations_overview uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({
            "api_base": _api_base(request),
            "keys": [_key_public(k) for k in keys],
            "max_keys": _keys.MAX_ACTIVE_KEYS,
            "scopes": [{"id": s, "label": _keys.SCOPE_LABELS[s]} for s in _keys.SCOPES],
            "endpoints": endpoints,
            "events": [{"id": e, "label": lbl} for e, lbl in _wh.EVENTS.items()
                       if e != "webhook.test"],
            "sheets": sheets,
            "datasets": [{"id": k, "label": v} for k, v in _gs.DATASETS.items()],
        })

    # ── Ключи API ────────────────────────────────────────────────────────────

    async def api_keys_list(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            keys = await _keys.list_keys(pool, uid)
        except Exception:
            log.exception("api_keys_list uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"keys": [_key_public(k) for k in keys]})

    async def create_api_key(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        d = await _body(request)
        try:
            k = await _keys.create_key(pool, uid, _str(d.get("name"), 64) or "Ключ мини-аппа",
                                       d.get("scopes"))
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("create_api_key uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, **k})

    async def revoke_api_key(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        key_id = _int_id(request, "key_id")
        if not key_id:
            return _err("Неверный идентификатор ключа", 400)
        try:
            if not await _keys.revoke_key(pool, uid, key_id):
                return _err("Не найдено", 404)
        except Exception:
            log.exception("revoke_api_key uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True})

    # ── Вебхуки ──────────────────────────────────────────────────────────────

    async def webhook_create(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        d = await _body(request)
        events = d.get("events") if isinstance(d.get("events"), (list, str)) else None
        try:
            ep = await _wh.create_endpoint(
                pool, uid, kind=_str(d.get("kind"), 16) or "http",
                name=_str(d.get("name"), 64), url=_str(d.get("url")),
                spreadsheet=_str(d.get("spreadsheet")),
                sheet_name=_str(d.get("sheet_name"), 100), events=events)
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("webhook_create uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        secret = ep.pop("secret", None)
        return _json_resp({"ok": True, "endpoint": ep, "signing_secret": secret})

    async def webhook_update(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wh_id = _int_id(request, "wh_id")
        if not wh_id:
            return _err("Неверный идентификатор получателя", 400)
        d = await _body(request)
        events = d.get("events") if isinstance(d.get("events"), (list, str)) else None
        active = d.get("is_active") if isinstance(d.get("is_active"), bool) else None
        name = d.get("name") if isinstance(d.get("name"), str) else None
        try:
            ep = await _wh.update_endpoint(pool, uid, wh_id, is_active=active,
                                           events=events, name=name)
        except Exception:
            log.exception("webhook_update uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not ep:
            return _err("Получатель не найден", 404)
        return _json_resp({"ok": True, "endpoint": ep})

    async def webhook_delete(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wh_id = _int_id(request, "wh_id")
        if not wh_id:
            return _err("Неверный идентификатор получателя", 400)
        try:
            if not await _wh.delete_endpoint(pool, uid, wh_id):
                return _err("Получатель не найден", 404)
        except Exception:
            log.exception("webhook_delete uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True})

    async def webhook_test(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wh_id = _int_id(request, "wh_id")
        if not wh_id:
            return _err("Неверный идентификатор получателя", 400)
        import aiohttp
        try:
            async with aiohttp.ClientSession(trust_env=True) as http:
                res = await _wh.send_test(pool, http, uid, wh_id)
        except Exception:
            log.exception("webhook_test uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if res.get("error") == "Получатель не найден":
            return _err(res["error"], 404)
        return _json_resp(res)

    async def webhook_rotate(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wh_id = _int_id(request, "wh_id")
        if not wh_id:
            return _err("Неверный идентификатор получателя", 400)
        try:
            secret = await _wh.rotate_secret(pool, uid, wh_id)
        except Exception:
            log.exception("webhook_rotate uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not secret:
            return _err("Получатель не найден", 404)
        return _json_resp({"ok": True, "signing_secret": secret})

    async def webhook_deliveries(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        wh_id = _int_id(request, "wh_id")
        if not wh_id:
            return _err("Неверный идентификатор получателя", 400)
        try:
            ep = await _wh.get_endpoint(pool, uid, wh_id)
            if not ep:
                return _err("Получатель не найден", 404)
            # Журнал — последние доставки, а не вся история (её чистит
            # prune через 14 дней): экран показывает, сколько всего, чтобы
            # обрезка не выглядела полной лентой.
            items = await _wh.list_deliveries(pool, uid, wh_id, limit=_DELIVERY_PAGE)
            total = await _wh.count_deliveries(pool, uid, wh_id)
        except Exception:
            log.exception("webhook_deliveries uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"endpoint": ep, "deliveries": items, "total": total,
                           "limit": _DELIVERY_PAGE})

    async def webhook_redeliver(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        delivery_id = _int_id(request, "delivery_id")
        if not delivery_id:
            return _err("Неверный идентификатор доставки", 400)
        try:
            if not await _wh.redeliver(pool, uid, delivery_id):
                return _err("Доставка не найдена или уже в очереди", 404)
        except Exception:
            log.exception("webhook_redeliver uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True})

    # ── Google Sheets ────────────────────────────────────────────────────────

    async def sheets_connect(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        d = await _body(request)
        raw = d.get("credentials")
        if not isinstance(raw, (str, dict)) or not raw:
            return _err("Вставьте JSON-ключ сервисного аккаунта", 400)
        try:
            conn = await _gs.save_connection(pool, uid, raw)
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sheets_connect uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "sheets": conn})

    async def sheets_disconnect(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            await _gs.delete_connection(pool, uid)
        except Exception:
            log.exception("sheets_disconnect uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True})

    async def sheets_check(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        d = await _body(request)
        try:
            res = await _gs.check_access(pool, uid, _str(d.get("spreadsheet")))
        except (ValueError, _gs.SheetsError) as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sheets_check uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp(res)

    async def sheets_export(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        d = await _body(request)
        try:
            res = await _gs.export_dataset(pool, uid, _str(d.get("spreadsheet")),
                                           _str(d.get("dataset"), 32))
        except (ValueError, _gs.SheetsError) as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("sheets_export uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, **res})

    app.router.add_get("/api/miniapp/integrations", integrations_overview)
    app.router.add_get("/api/miniapp/api_keys", api_keys_list)
    app.router.add_post("/api/miniapp/api_key", create_api_key)
    app.router.add_delete("/api/miniapp/api_key/{key_id}", revoke_api_key)
    app.router.add_post("/api/miniapp/webhooks", webhook_create)
    app.router.add_post("/api/miniapp/webhooks/{wh_id}", webhook_update)
    app.router.add_delete("/api/miniapp/webhooks/{wh_id}", webhook_delete)
    app.router.add_post("/api/miniapp/webhooks/{wh_id}/test", webhook_test)
    app.router.add_post("/api/miniapp/webhooks/{wh_id}/rotate", webhook_rotate)
    app.router.add_get("/api/miniapp/webhooks/{wh_id}/deliveries", webhook_deliveries)
    app.router.add_post("/api/miniapp/webhook_delivery/{delivery_id}/retry", webhook_redeliver)
    app.router.add_post("/api/miniapp/sheets/connect", sheets_connect)
    app.router.add_post("/api/miniapp/sheets/disconnect", sheets_disconnect)
    app.router.add_post("/api/miniapp/sheets/check", sheets_check)
    app.router.add_post("/api/miniapp/sheets/export", sheets_export)

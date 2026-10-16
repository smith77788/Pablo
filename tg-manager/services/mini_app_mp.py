"""Витрина провайдеров — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: 12 маршрутов `/api/miniapp/mp/*` —
регистрация провайдера, его карточка и правка, ключи API (выдача и отзыв),
услуги (создание, правка, включение), каталог и админский срез.

`_mp_owned_provider` и `_mp_owned_service` уехали вместе с ручками: это и есть
скоуп владельца в этом экране, других потребителей у них нет. Сырые ключи API
здесь не живут и не отдаются — `provider_marketplace.list_api_keys` возвращает
только префиксы, а полный ключ виден ОДИН раз при выдаче.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты витрины провайдеров на приложении мини-аппа."""
    from services import provider_marketplace as _mp
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _is_admin, _json_resp, _list_limit,
        _list_offset, int_field, log,
    )

    async def _mp_owned_provider(uid, pid):
        p = await _mp.get_provider(pool, pid)
        return p if (p and int(p["owner_id"]) == int(uid)) else None

    async def _mp_owned_service(uid, sid):
        s = await _mp.get_service(pool, sid)
        if not s:
            return None, None
        p = await _mp_owned_provider(uid, s["provider_id"])
        return (s, p) if p else (None, None)

    async def mp_register(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            d = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос", 400)
        try:
            p = await _mp.register_provider(
                pool, uid, d.get("name", ""), description=d.get("description", ""),
                category=d.get("category", "other"), website=d.get("website", ""),
                contact=d.get("contact", ""))
            return _json_resp({"ok": True, "provider": p})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("mp_register uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_mine(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            return _json_resp({"providers": await _mp.list_providers(pool, owner_id=uid)})
        except Exception:
            log.exception("mp_mine uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_provider_detail(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        p = await _mp_owned_provider(uid, pid)
        if not p:
            return _err("Не найдено", 404)
        return _json_resp({"provider": p,
                           "services": await _mp.list_services(pool, pid),
                           "api_keys": await _mp.list_api_keys(pool, pid)})

    async def mp_provider_update(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _mp_owned_provider(uid, pid):
            return _err("Не найдено", 404)
        try:
            p = await _mp.update_provider_profile(pool, pid, uid, **d)
            return _json_resp({"ok": True, "provider": p})
        except Exception:
            log.exception("mp_provider_update uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_apikey_issue(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        if not await _mp_owned_provider(uid, pid):
            return _err("Не найдено", 404)
        try:
            # api_key возвращается ОДИН раз — фронт обязан показать и попросить сохранить
            return _json_resp({"ok": True, "key": await _mp.issue_api_key(
                pool, pid, actor_id=uid)})
        except Exception:
            log.exception("mp_apikey_issue uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_apikey_revoke(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            kid = int(request.match_info["kid"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        if not await _mp_owned_provider(uid, pid):
            return _err("Не найдено", 404)
        ok = await _mp.revoke_api_key(pool, pid, kid, actor_id=uid)
        return _json_resp({"ok": ok})

    async def mp_service_create(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        if not await _mp_owned_provider(uid, pid):
            return _err("Не найдено", 404)
        try:
            price_cents = (int_field(d["price_cents"], "Цена")
                           if d.get("price_cents") is not None
                           else _mp.to_cents(d.get("price", "0")))
            s = await _mp.create_service(
                pool, pid, title=d.get("title", ""), category=d.get("category", "other"),
                resource_kind=d.get("resource_kind", ""),
                description=d.get("description", ""), price_cents=price_cents,
                currency=d.get("currency", "USD"), unit=d.get("unit", "unit"),
                min_qty=int_field(d.get("min_qty"), "Минимальный объём", default=1),
                max_qty=int_field(d.get("max_qty"), "Максимальный объём",
                                  default=1000000),
                stock=int_field(d.get("stock"), "Остаток", default=-1),
                sla_hours=int_field(d.get("sla_hours"), "Срок исполнения, часов",
                                    default=24),
                params_schema=d.get("params_schema") or [], actor_id=uid)
            return _json_resp({"ok": True, "service": s})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("mp_service_create uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_service_update(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sid = int(request.match_info["sid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        s, p = await _mp_owned_service(uid, sid)
        if not p:
            return _err("Не найдено", 404)
        try:
            if d.get("price") is not None and d.get("price_cents") is None:
                d["price_cents"] = _mp.to_cents(d.pop("price"))
            upd = await _mp.update_service(pool, sid, p["id"], actor_id=uid, **d)
            return _json_resp({"ok": True, "service": upd})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("mp_service_update uid=%s sid=%s", uid, sid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_service_toggle(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            sid = int(request.match_info["sid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        s, p = await _mp_owned_service(uid, sid)
        if not p:
            return _err("Не найдено", 404)
        ok = await _mp.set_service_active(pool, sid, p["id"], bool(d.get("active", True)),
                                          actor_id=uid)
        return _json_resp({"ok": ok})

    async def mp_catalog(request):
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        q = request.query
        try:
            items = await _mp.browse_catalog(
                pool, category=q.get("category") or None,
                resource_kind=q.get("kind") or None, q=q.get("q") or None,
                limit=_list_limit(request, 50, 200), offset=_list_offset(request))
            return _json_resp({"items": items})
        except Exception:
            log.exception("mp_catalog uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_admin_status(request):
        uid = _get_uid(request)
        if not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        try:
            pid = int(request.match_info["pid"])
            d = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        try:
            p = await _mp.set_provider_status(
                pool, pid, d.get("status", ""), by=uid, reason=d.get("reason", ""))
            if not p:
                return _err("Не найдено", 404)
            return _json_resp({"ok": True, "provider": p})
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("mp_admin_status uid=%s pid=%s", uid, pid)
            return _err(_INTERNAL_ERROR, 500)

    async def mp_admin_providers(request):
        uid = _get_uid(request)
        if not _is_admin(uid):
            return _err("Раздел только для владельца платформы", 403)
        return _json_resp({"providers": await _mp.list_providers(
            pool, status=request.query.get("status") or None)})

    app.router.add_post("/api/miniapp/mp/register", mp_register)
    app.router.add_get("/api/miniapp/mp/mine", mp_mine)
    app.router.add_get("/api/miniapp/mp/catalog", mp_catalog)
    app.router.add_get("/api/miniapp/mp/provider/{pid}", mp_provider_detail)
    app.router.add_patch("/api/miniapp/mp/provider/{pid}", mp_provider_update)
    app.router.add_post("/api/miniapp/mp/provider/{pid}/apikey", mp_apikey_issue)
    app.router.add_post("/api/miniapp/mp/provider/{pid}/apikey/{kid}/revoke", mp_apikey_revoke)
    app.router.add_post("/api/miniapp/mp/provider/{pid}/service", mp_service_create)
    app.router.add_patch("/api/miniapp/mp/service/{sid}", mp_service_update)
    app.router.add_post("/api/miniapp/mp/service/{sid}/toggle", mp_service_toggle)
    app.router.add_get("/api/miniapp/mp/admin/providers", mp_admin_providers)
    app.router.add_post("/api/miniapp/mp/admin/provider/{pid}/status", mp_admin_status)

"""Host-Server: покупаемый модуль и маркетплейс инфраструктуры.

Первая группа маршрутов, вынесенная из `services/mini_app_api.py`. Тот файл —
24 тысячи строк и 926 маршрутов в одной функции `setup_routes`; искать в нём
дороже, чем править, и это заметная часть расхода на каждую задачу по мини-аппу.
Режем по одной группе за раз, и только такие, что не завязаны на соседей: здесь
обработчики опираются лишь на общие помощники и на `services/host_server.py`.

Что маршруты никуда не пропали, стережёт `tests/test_miniapp_routes_are_not_lost.py`
— слепок всех зарегистрированных маршрутов, снятый ДО разреза.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Регистрирует маршруты Host-Server на общем приложении мини-аппа."""
    # Общие помощники живут в mini_app_api, а он импортирует этот модуль —
    # импорт сверху дал бы цикл. Внутри функции его нет: к моменту вызова
    # mini_app_api уже загружен. Когда таких модулей станет несколько, помощники
    # переедут в общий модуль и этот импорт уйдёт.
    from services.mini_app_api import _err, _get_uid, _is_admin, _json_resp, log

    async def host_server_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import host_server as hs
            return _json_resp({
                "has_access": await hs.has_access(pool, uid),
                "price_usd": await hs.get_price(pool),
                "is_admin": _is_admin(uid),
                "kinds": list(hs.OFFERING_KINDS),
                "periods": list(hs.PERIODS),
                "device_exec_enabled": hs.device_compute_execution_enabled(),
            })
        except Exception:
            log.exception("host_server_status uid=%s", uid)
            return _err("Не удалось получить состояние модуля", 500)

    async def host_server_buy(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import host_server as hs
            res = await hs.create_purchase(pool, uid)
            if res.get("already"):
                return _json_resp({"ok": True, "already": True})
            return _json_resp({"ok": True, **res})
        except Exception:
            log.exception("host_server_buy uid=%s", uid)
            return _err("Не удалось оформить покупку", 500)

    async def host_server_set_price(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        if not _is_admin(uid):
            return _err("Только для администратора", 403)
        try:
            body = await request.json()
            from services import host_server as hs
            applied = await hs.set_price(pool, body.get("price_usd"))
            return _json_resp({"ok": True, "price_usd": applied})
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception:
            log.exception("host_server_set_price uid=%s", uid)
            return _err("Не удалось изменить цену", 500)

    async def host_server_market(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import host_server as hs
            kind = request.query.get("kind") or None
            offers = await hs.list_market(pool, viewer_id=uid, kind=kind)
            return _json_resp({"offerings": offers})
        except Exception:
            log.exception("host_server_market uid=%s", uid)
            return _err("Не удалось загрузить предложения", 500)

    async def host_server_create_offering(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            from services import host_server as hs
            res = await hs.create_offering(
                pool, uid,
                kind=body.get("kind", "server"),
                title=body.get("title", ""),
                price_usd=body.get("price_usd", 0),
                period=body.get("period", "month"),
                description=body.get("description"),
                specs=body.get("specs"),
                region=body.get("region"),
                allow_device_compute=bool(body.get("allow_device_compute")),
            )
            return _json_resp({"ok": True, **res})
        except PermissionError as pe:
            return _err(str(pe), 403)
        except ValueError as ve:
            return _err(str(ve), 400)
        except Exception:
            log.exception("host_server_create_offering uid=%s", uid)
            return _err("Не удалось создать предложение", 500)

    async def host_server_toggle_offering(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            offering_id = int(request.match_info["offering_id"])
            body = await request.json()
            from services import host_server as hs
            ok = await hs.set_offering_active(pool, uid, offering_id, bool(body.get("active", True)))
            if not ok:
                return _err("Предложение не найдено", 404)
            return _json_resp({"ok": True})
        except (KeyError, ValueError):
            return _err("Неверный идентификатор предложения", 400)
        except Exception:
            log.exception("host_server_toggle_offering uid=%s", uid)
            return _err("Не удалось переключить предложение", 500)

    async def host_server_my(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import host_server as hs
            return _json_resp({
                "offerings": await hs.my_offerings(pool, uid),
                "rentals_out": await hs.incoming_rentals(pool, uid),
                "rentals_in": await hs.my_rentals(pool, uid),
            })
        except Exception:
            log.exception("host_server_my uid=%s", uid)
            return _err("Не удалось загрузить ваши предложения и аренды", 500)

    async def host_server_rent(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
            offering_id = int(body.get("offering_id"))
            units = int(body.get("units") or 1)
            from services import host_server as hs
            res = await hs.rent_offering(pool, uid, offering_id, units=units)
            return _json_resp({"ok": True, **res})
        except (TypeError, ValueError) as ve:
            return _err(str(ve) or "Неверный запрос", 400)
        except Exception:
            log.exception("host_server_rent uid=%s", uid)
            return _err("Не удалось арендовать", 500)

    async def host_server_rental_status(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rental_id = int(request.match_info["rental_id"])
            body = await request.json()
            new_status = body.get("status", "")
            as_provider = bool(body.get("as_provider", True))
            from services import host_server as hs
            res = await hs.set_rental_status(
                pool, uid, rental_id, new_status, as_provider=as_provider
            )
            return _json_resp({"ok": True, **res})
        except PermissionError as pe:
            return _err(str(pe), 403)
        except ValueError as ve:
            return _err(str(ve), 400)
        except KeyError:
            return _err("Неверный идентификатор аренды", 400)
        except Exception:
            log.exception("host_server_rental_status uid=%s", uid)
            return _err("Не удалось изменить состояние аренды", 500)

    app.router.add_get("/api/miniapp/host_server/status", host_server_status)
    app.router.add_post("/api/miniapp/host_server/buy", host_server_buy)
    app.router.add_post("/api/miniapp/host_server/price", host_server_set_price)
    app.router.add_get("/api/miniapp/host_server/market", host_server_market)
    app.router.add_post("/api/miniapp/host_server/offering", host_server_create_offering)
    app.router.add_post("/api/miniapp/host_server/offering/{offering_id}/toggle", host_server_toggle_offering)
    app.router.add_get("/api/miniapp/host_server/my", host_server_my)
    app.router.add_post("/api/miniapp/host_server/rent", host_server_rent)
    app.router.add_post("/api/miniapp/host_server/rental/{rental_id}/status", host_server_rental_status)

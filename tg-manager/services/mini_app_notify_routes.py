"""Каналы уведомлений о событиях ботов — маршруты мини-аппа.

GET    /api/miniapp/notify_routes            — маршруты, куда можно слать, боты
POST   /api/miniapp/notify_routes            — создать / изменить (id в теле)
DELETE /api/miniapp/notify_routes/{route_id} — удалить
POST   /api/miniapp/notify_routes/{route_id}/test — тестовое сообщение

Канал-приёмник — только тот, где ГЛАВНЫЙ бот админ и который этот владелец
подключил («Модератор чатов» пишет его в guard_chats при выдаче админки).
Чужой chat_id подставить нельзя: проверка по owner_id, плюс живой getChatMember.
"""
from __future__ import annotations

from aiohttp import web

_bot_username_cache: dict[str, str] = {}


async def _main_call(method: str, **params) -> dict:
    """Вызов Bot API главным ботом (без объекта aiogram в веб-процессе)."""
    import aiohttp

    from config import BOT_TOKEN
    from services import bot_api

    async with aiohttp.ClientSession() as http:
        return await bot_api._call(http, BOT_TOKEN, method, **params)


async def _main_username() -> str:
    if "u" not in _bot_username_cache:
        try:
            me = (await _main_call("getMe")).get("result") or {}
            if me.get("username"):
                _bot_username_cache["u"] = me["username"]
        except Exception:
            return ""
    return _bot_username_cache.get("u", "")


def setup_routes(app: web.Application, pool) -> None:
    from services import bot_notify_routes as R
    from services.mini_app_api import _err, _get_uid, _json_resp, _safe_fetch, log

    async def _chats(uid: int) -> list[dict]:
        rows = await _safe_fetch(pool,
            "SELECT chat_id, title, username FROM guard_chats "
            "WHERE owner_id=$1 AND is_active ORDER BY updated_at DESC LIMIT 200", uid)
        return [{"chat_id": int(r["chat_id"]),
                 "title": r.get("title") or (("@" + r["username"]) if r.get("username") else str(r["chat_id"]))}
                for r in rows]

    async def _bots(uid: int) -> list[dict]:
        rows = await _safe_fetch(pool,
            "SELECT bot_id, username, first_name FROM managed_bots "
            "WHERE added_by=$1 AND is_active ORDER BY bot_id LIMIT 500", uid)
        return [{"bot_id": int(r["bot_id"]),
                 "title": ("@" + r["username"]) if r.get("username") else (r.get("first_name") or str(r["bot_id"]))}
                for r in rows]

    async def routes_get(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            routes = await R.list_routes(pool, uid)
        except Exception:
            log.exception("notify_routes list uid=%s", uid)
            return _err("Не удалось загрузить каналы уведомлений", 500)
        for r in routes:
            if r.get("updated_at") is not None:
                r["updated_at"] = r["updated_at"].isoformat()
        uname = await _main_username()
        return _json_resp({
            "ok": True, "routes": routes, "chats": await _chats(uid),
            "bots": await _bots(uid), "events": R.EVENT_LABELS,
            "add_url": (f"https://t.me/{uname}?startchannel=notify&admin=post_messages+invite_users"
                        if uname else ""),
            "bot_username": uname,
        })

    async def routes_save(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            body = await request.json()
        except Exception:
            return _err("Неверный запрос", 400)
        route, err = R.normalize(body if isinstance(body, dict) else {})
        if err:
            return _err(err, 400)
        chats = {c["chat_id"]: c["title"] for c in await _chats(uid)}
        if route["chat_id"] not in chats:
            return _err("Этот канал не подключён: добавьте в него бота администратором", 400)
        if route["bot_ids"] is not None:
            mine = {b["bot_id"] for b in await _bots(uid)} | {0}
            if not set(route["bot_ids"]) <= mine:
                return _err("В списке есть чужой или удалённый бот", 400)
        # Бот может писать в канал? Иначе маршрут молча ничего бы не доставлял.
        try:
            me = (await _main_call("getMe")).get("result") or {}
            m = await _main_call("getChatMember", chat_id=route["chat_id"],
                                 user_id=int(me.get("id") or 0))
            res = m.get("result") or {}
            if m.get("ok") and (res.get("status") != "administrator"
                                or res.get("can_post_messages") is False):
                return _err("У бота нет права публиковать в этом канале — "
                            "выдайте «Публикация сообщений»", 400)
        except Exception:
            log.debug("notify_routes: проверка прав пропущена", exc_info=True)
        route_id = body.get("id")
        try:
            rid = await R.save_route(pool, uid, route, chats[route["chat_id"]],
                                     int(route_id) if route_id else None)
        except ValueError as e:
            return _err(str(e), 400)
        except Exception:
            log.exception("notify_routes save uid=%s", uid)
            return _err("Не удалось сохранить", 500)
        if not rid:
            return _err("Канал уведомлений не найден", 404)
        return _json_resp({"ok": True, "id": int(rid)})

    async def routes_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["route_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        if not await R.delete_route(pool, uid, rid):
            return _err("Канал уведомлений не найден", 404)
        return _json_resp({"ok": True})

    async def routes_test(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            rid = int(request.match_info["route_id"])
        except (KeyError, ValueError):
            return _err("Неверный идентификатор", 400)
        route = next((r for r in await R.list_routes(pool, uid) if int(r["id"]) == rid), None)
        if not route:
            return _err("Канал уведомлений не найден", 404)
        name = route.get("name") or "без названия"
        r = await _main_call(
            "sendMessage", chat_id=int(route["chat_id"]), parse_mode="HTML",
            text=("🔔 <b>Проверка канала уведомлений</b>\n"
                  f"Сюда будут приходить события сети «{_esc(name)}»."))
        if not r.get("ok"):
            msg = R.send_error_ru(Exception(str(r.get("description") or "")))
            await R._mark(pool, rid, msg)
            return _err(msg, 400)
        await R._mark(pool, rid, None)
        return _json_resp({"ok": True})

    app.router.add_get("/api/miniapp/notify_routes", routes_get)
    app.router.add_post("/api/miniapp/notify_routes", routes_save)
    app.router.add_delete("/api/miniapp/notify_routes/{route_id}", routes_delete)
    app.router.add_post("/api/miniapp/notify_routes/{route_id}/test", routes_test)


def _esc(s: str) -> str:
    import html
    return html.escape(s or "")

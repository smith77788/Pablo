"""«🎯 План роста» в мини-аппе: цели по каналам и ботам.

Логика — services/growth_plan.py; здесь только двери. Цель и её канал/бот
сверяются с владельцем запросом к базе: чужое — «не найдено».
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    from services import growth_plan as gp
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, bad_body_response, log,
    )

    async def _goal_body(request):
        try:
            data = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return None, (bad if bad is not None else _err("Не удалось разобрать запрос", 400))
        if not isinstance(data, dict):
            return None, _err("Не удалось разобрать запрос", 400)
        return data, None

    async def _own_goal(uid: int, gid) -> dict | None:
        return await gp.get_goal(pool, uid, gid)

    async def growth_goals(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            return _json_resp({"goals": await gp.list_goals(pool, uid),
                               "assets": await gp.assets(pool, uid)})
        except Exception:
            log.exception("growth_goals uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)

    async def growth_goal_create(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _goal_body(request)
        if bad is not None:
            return bad
        try:
            gid = await gp.create_goal(pool, uid, data)
        except gp.GoalError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("growth_goal_create uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True, "id": gid})

    async def growth_goal_plan(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        goal = await _own_goal(uid, request.match_info.get("gid"))
        if not goal:
            return _err("Цель не найдена", 404)
        try:
            return _json_resp(await gp.plan(pool, uid, goal))
        except Exception:
            log.exception("growth_goal_plan uid=%s gid=%s", uid, goal["id"])
            return _err(_INTERNAL_ERROR, 500)

    async def growth_goal_update(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        data, bad = await _goal_body(request)
        if bad is not None:
            return bad
        goal = await _own_goal(uid, request.match_info.get("gid"))
        if not goal:
            return _err("Цель не найдена", 404)
        try:
            await gp.update_goal(pool, uid, goal, data)
        except gp.GoalError as exc:
            return _err(str(exc), 400)
        except Exception:
            log.exception("growth_goal_update uid=%s gid=%s", uid, goal["id"])
            return _err(_INTERNAL_ERROR, 500)
        return _json_resp({"ok": True})

    async def growth_goal_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        goal = await _own_goal(uid, request.match_info.get("gid"))
        if not goal:
            return _err("Цель не найдена", 404)
        await gp.delete_goal(pool, uid, goal["id"])
        return _json_resp({"ok": True})

    app.router.add_get("/api/miniapp/growth/goals", growth_goals)
    app.router.add_post("/api/miniapp/growth/goal", growth_goal_create)
    app.router.add_get("/api/miniapp/growth/goal/{gid}/plan", growth_goal_plan)
    app.router.add_post("/api/miniapp/growth/goal/{gid}/update", growth_goal_update)
    app.router.add_post("/api/miniapp/growth/goal/{gid}/delete", growth_goal_delete)

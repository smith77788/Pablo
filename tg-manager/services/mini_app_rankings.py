"""Замер позиций каналов в поиске — маршруты мини-аппа.

Вынесено из `services/mini_app_api.py`: четыре маршрута
`/api/miniapp/channel_rankings*` — список ключей с последней и предыдущей
позицией, добавить ключ, снять ключ с отслеживания, запустить замер.

Семейство связное: вся его работа живёт в `services/channel_ranking`, а сам
замер уходит в очередь операций; ручки только проверяют владение и переводят
строки запроса в вызов сервиса.

Проверка владения каналом (`SELECT 1 FROM managed_channels WHERE owner_id=$1
AND channel_id=$2`) перенесена дословно и остаётся в `channel_rankings_add`:
без неё ключ можно было бы завести на чужой канал.

Поведение при переносе не менялось: тела обработчиков и строки регистрации
маршрутов перенесены дословно.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    """Зарегистрировать маршруты замера позиций на приложении мини-аппа."""
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, _safe_fetchval, log,
    )
    from services.security import validate_string

    async def channel_rankings_list(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        from services import channel_ranking
        rows = await channel_ranking.list_positions(pool, uid)
        out = [{
            "id": r["id"], "channel_id": str(r["channel_id"]),
            "keyword": r["keyword"], "title": r.get("title") or "",
            "username": r.get("username") or "",
            "position": r.get("pos"), "prev_position": r.get("prev_pos"),
            "checked_at": str(r["checked_at"]) if r.get("checked_at") else None,
        } for r in rows]
        return _json_resp({"keywords": out})

    async def channel_rankings_add(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception:
            return _err("Не удалось разобрать запрос")
        try:
            channel_id = int(data.get("channel_id"))
        except (TypeError, ValueError):
            return _err("Неверный канал")
        keyword = validate_string(data.get("keyword"), max_len=64) or ""
        if not keyword:
            return _err("Введите ключевое слово")
        # Канал должен принадлежать владельцу.
        owns = await _safe_fetchval(
            pool, "SELECT 1 FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
            uid, channel_id)
        if not owns:
            return _err("Канал не найден среди ваших", 404)
        from services import channel_ranking
        ok = await channel_ranking.add_keyword(pool, uid, channel_id, keyword)
        return _json_resp({"ok": bool(ok)})

    async def channel_rankings_remove(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
            kid = int(data.get("id"))
        except (TypeError, ValueError, Exception):
            return _err("Неверный идентификатор")
        from services import channel_ranking
        ok = await channel_ranking.remove_keyword(pool, uid, kid)
        return _json_resp({"ok": bool(ok)})

    async def channel_rankings_check(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            from services import operation_bus
            op_id = await operation_bus.submit(
                pool, uid, "check_channel_rankings", {},
                label="Замер позиций каналов в поиске")
            return _json_resp({"ok": True, "op_id": op_id})
        except PermissionError as exc:
            return _err(str(exc) or "Требуется подписка", 403)
        except Exception:
            log.exception("channel_rankings_check uid=%d", uid)
            return _err(_INTERNAL_ERROR, 500)

    app.router.add_get("/api/miniapp/channel_rankings", channel_rankings_list)
    app.router.add_post("/api/miniapp/channel_rankings/add", channel_rankings_add)
    app.router.add_post("/api/miniapp/channel_rankings/remove", channel_rankings_remove)
    app.router.add_post("/api/miniapp/channel_rankings/check", channel_rankings_check)

"""Подписчик бота → карточка контакта одним нажатием.

ЗАЧЕМ. «Новые подписчики» были тупиком: строку нельзя было нажать — ни
открыть человека, ни написать ему, ни завести сделку. Контакт по Telegram-id
при этом заводится одной дверью (repository.ensure_contact_by_telegram_id) —
её и зовём; карточка контакта уже умеет всё остальное (CRM, сделка, история).

Скоуп: бот обязан принадлежать владельцу, а человек — быть подписчиком ЭТОГО
бота. Иначе по чужому bot_id можно было бы завести себе карточку на
постороннего человека.
"""
from __future__ import annotations

from aiohttp import web


def setup_routes(app: web.Application, pool) -> None:
    from services.contacts_hub.repository import ensure_contact_by_telegram_id
    from services.mini_app_api import (
        _INTERNAL_ERROR, _err, _get_uid, _json_resp, bad_body_response, log,
    )
    from services.security import tg_id

    async def bot_user_contact(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return bad if bad is not None else _err("Не удалось разобрать запрос", 400)
        bot_id = tg_id((data or {}).get("bot_id"), positive=True)
        user_id = tg_id((data or {}).get("user_id"), positive=True)
        if not bot_id or not user_id:
            return _err("Не указан бот или подписчик", 400)
        row = await pool.fetchrow(
            "SELECT bu.username, bu.first_name FROM bot_users bu "
            "JOIN managed_bots mb ON mb.bot_id = bu.bot_id "
            "WHERE bu.bot_id=$1 AND bu.user_id=$2 AND mb.added_by=$3",
            bot_id, user_id, uid)
        if not row:
            return _err("Этот человек не подписчик вашего бота", 404)
        try:
            cid = await ensure_contact_by_telegram_id(
                pool, uid, user_id, username=row["username"], first_name=row["first_name"])
        except Exception:
            log.exception("bot_user_contact uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        if not cid:
            return _err("Не удалось завести контакт — попробуйте ещё раз", 500)
        return _json_resp({"ok": True, "contact_id": str(cid)})

    app.router.add_post("/api/miniapp/bot_user/contact", bot_user_contact)

"""
REST API для внешних интеграций (n8n, Zapier и т.п.).

Маршруты добавляются к существующему aiohttp-приложению payment_webhook.

Endpoints:
  GET  /api/v1/health
  GET  /api/v1/accounts?owner_id=X
  POST /api/v1/send_message
  POST /api/v1/click_button
  GET  /api/v1/get_messages?owner_id=X&account_id=Y&chat_id=Z&limit=N

Аутентификация:
  Header: X-Api-Key: <ADMIN_SECRET>
  или:    Authorization: Bearer <ADMIN_SECRET>
"""

from __future__ import annotations

import logging

import asyncpg
from aiohttp import web
from aiogram import Bot

from config import ADMIN_SECRET
from database import db
from services import account_manager
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)


def _check_auth(request: web.Request) -> bool:
    if not ADMIN_SECRET:
        return True
    key = (
        request.headers.get("X-Api-Key", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    return key == ADMIN_SECRET


def _unauth() -> web.Response:
    return web.json_response({"error": "Unauthorized"}, status=401)


def _bad(msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=400)


async def _click_inline_button(
    session_str: str,
    chat_id: int,
    message_id: int,
    button_data: str,
    _acc: dict | None = None,
) -> dict:
    """Нажать inline-кнопку от имени аккаунта через Telethon."""
    from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

    client = account_manager._make_client(session_str, _acc)
    try:
        await client.connect()
        peer = await client.get_input_entity(chat_id)
        result = await client(
            GetBotCallbackAnswerRequest(
                peer=peer,
                msg_id=message_id,
                data=button_data.encode()
                if isinstance(button_data, str)
                else button_data,
            )
        )
        return {"ok": True, "alert": result.alert, "message": result.message or ""}
    finally:
        await client.disconnect()


async def _get_chat_messages(
    session_str: str,
    chat_id: int,
    limit: int = 20,
    _acc: dict | None = None,
) -> list[dict]:
    """Получить последние сообщения из чата (включая ЛС с ботом)."""
    from telethon.errors import (
        ChannelPrivateError,
        FloodWaitError,
        ChatWriteForbiddenError,
    )

    client = account_manager._make_client(session_str, _acc)
    result = []
    try:
        await client.connect()
        try:
            msgs = await client.get_messages(chat_id, limit=limit)
        except ChannelPrivateError:
            log.warning("_get_chat_messages: ChannelPrivate chat_id=%s", chat_id)
            await client.disconnect()
            return []
        except FloodWaitError as e:
            log.warning("_get_chat_messages: FloodWait %ds chat_id=%s", e.seconds, chat_id)
            await client.disconnect()
            return []
        except ChatWriteForbiddenError:
            log.warning("_get_chat_messages: no write access chat_id=%s", chat_id)
            # Still try to read messages
            try:
                msgs = await client.get_messages(chat_id, limit=limit)
            except Exception as e:
                log.warning("_get_chat_messages: read failed chat_id=%s: %s", chat_id, e)
                await client.disconnect()
                return []
        except Exception as e:
            log.warning("_get_chat_messages: unexpected error chat_id=%s: %s", chat_id, e)
            await client.disconnect()
            return []

        for m in msgs:
            entry: dict = {
                "id": m.id,
                "date": m.date.isoformat() if m.date else None,
                "text": m.message or "",
                "out": m.out,
                "from_id": None,
            }
            if m.sender_id:
                entry["from_id"] = m.sender_id
            if m.reply_markup:
                buttons = []
                for row in (
                    m.reply_markup.rows if hasattr(m.reply_markup, "rows") else []
                ):
                    for btn in row.buttons:
                        b: dict = {"text": btn.text}
                        if hasattr(btn, "data") and btn.data:
                            b["data"] = (
                                btn.data.decode()
                                if isinstance(btn.data, bytes)
                                else btn.data
                            )
                        buttons.append(b)
                if buttons:
                    entry["buttons"] = buttons
            result.append(entry)
    finally:
        await client.disconnect()
    return result


def add_routes(app: web.Application, pool: asyncpg.Pool, bot: Bot) -> None:
    """Добавить REST API маршруты к существующему aiohttp-приложению."""

    async def api_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "Infragram REST API v1"})

    async def api_accounts(request: web.Request) -> web.Response:
        if not _check_auth(request):
            return _unauth()
        try:
            owner_id = int(request.query["owner_id"])
        except (KeyError, ValueError):
            return _bad("owner_id required")
        try:
            rows = await db.get_tg_accounts(pool, owner_id)
            return web.json_response(
                [
                    {
                        "id": r["id"],
                        "phone": r["phone"],
                        "first_name": r.get("first_name", ""),
                        "username": r.get("username") or "",
                        "is_active": r.get("is_active", True),
                    }
                    for r in rows
                ]
            )
        except Exception:
            log_exc_swallow(log, "rest_api: api_accounts error")
            return web.json_response({"error": "internal error"}, status=500)

    async def api_send_message(request: web.Request) -> web.Response:
        if not _check_auth(request):
            return _unauth()
        try:
            data = await request.json()
        except Exception:
            return _bad("invalid JSON")
        owner_id = data.get("owner_id")
        account_id = data.get("account_id")
        chat_id = data.get("chat_id")
        text = data.get("text", "")
        if not all([owner_id, account_id, chat_id, text]):
            return _bad("owner_id, account_id, chat_id, text are required")
        try:
            acc = await db.get_account_for_telethon(
                pool, int(account_id), int(owner_id)
            )
            if not acc:
                return web.json_response({"error": "account not found"}, status=404)
            ok = await account_manager.send_message_via_account(
                acc["session_str"], int(chat_id), text, _acc=dict(acc)
            )
            return web.json_response({"ok": ok})
        except Exception:
            log_exc_swallow(log, "rest_api: api_send_message error")
            return web.json_response({"error": "internal error"}, status=500)

    async def api_click_button(request: web.Request) -> web.Response:
        if not _check_auth(request):
            return _unauth()
        try:
            data = await request.json()
        except Exception:
            return _bad("invalid JSON")
        owner_id = data.get("owner_id")
        account_id = data.get("account_id")
        chat_id = data.get("chat_id")
        message_id = data.get("message_id")
        button_data = data.get("button_data", "")
        if not all([owner_id, account_id, chat_id, message_id, button_data]):
            return _bad(
                "owner_id, account_id, chat_id, message_id, button_data are required"
            )
        try:
            acc = await db.get_account_for_telethon(
                pool, int(account_id), int(owner_id)
            )
            if not acc:
                return web.json_response({"error": "account not found"}, status=404)
            result = await _click_inline_button(
                acc["session_str"],
                int(chat_id),
                int(message_id),
                button_data,
                _acc=dict(acc),
            )
            return web.json_response(result)
        except Exception:
            log_exc_swallow(log, "rest_api: api_click_button error")
            return web.json_response({"error": "internal error"}, status=500)

    async def api_get_messages(request: web.Request) -> web.Response:
        if not _check_auth(request):
            return _unauth()
        try:
            owner_id = int(request.query["owner_id"])
            account_id = int(request.query["account_id"])
            chat_id = int(request.query["chat_id"])
        except (KeyError, ValueError):
            return _bad("owner_id, account_id, chat_id are required")
        limit = min(int(request.query.get("limit", "20")), 100)
        try:
            acc = await db.get_account_for_telethon(pool, account_id, owner_id)
            if not acc:
                return web.json_response({"error": "account not found"}, status=404)
            messages = await _get_chat_messages(
                acc["session_str"], chat_id, limit=limit, _acc=dict(acc)
            )
            return web.json_response({"messages": messages, "count": len(messages)})
        except Exception:
            log_exc_swallow(log, "rest_api: api_get_messages error")
            return web.json_response({"error": "internal error"}, status=500)

    app.router.add_get("/api/v1/health", api_health)
    app.router.add_get("/api/v1/accounts", api_accounts)
    app.router.add_post("/api/v1/send_message", api_send_message)
    app.router.add_post("/api/v1/click_button", api_click_button)
    app.router.add_get("/api/v1/get_messages", api_get_messages)

    log.info(
        "REST API routes registered: /api/v1/{health,accounts,send_message,click_button,get_messages}"
    )

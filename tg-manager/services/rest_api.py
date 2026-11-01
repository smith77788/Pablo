"""
REST API для внешних интеграций (n8n, Zapier, Make, свои скрипты).

Маршруты добавляются к существующему aiohttp-приложению payment_webhook.

Endpoints:
  GET  /api/v1/health
  GET  /api/v1/me                          — чей ключ и что он может
  GET  /api/v1/accounts
  GET  /api/v1/operations?status=&limit=   — операции владельца
  GET  /api/v1/operations/{op_id}
  POST /api/v1/send_message                (область write)
  POST /api/v1/click_button                (область write)
  GET  /api/v1/get_messages?account_id=Y&chat_id=Z&limit=N

Аутентификация (заголовок X-Api-Key или Authorization: Bearer):
  * ЛИЧНЫЙ ключ владельца (`bm_…`, выдаётся в мини-аппе «Интеграции» и в боте
    «API Hub»). Владелец берётся ИЗ КЛЮЧА; owner_id в запросе можно не
    передавать, а чужой owner_id — 403. Области: read / write.
  * ADMIN_SECRET — служебный ключ платформы: owner_id обязателен.

До этой правки личные ключи выдавались, но сюда не принимались — n8n с ключом
из мини-аппа получал 401 (см. докстринг services/api_keys.py).
"""

from __future__ import annotations

import logging
import re

import asyncpg
from aiohttp import web
from aiogram import Bot

from config import ADMIN_SECRET
from database import db
from services import account_manager
from services.logger import log_exc_swallow
from services.security import (
    check_rate_limit,
    rate_limit_response,
    validate_integer,
    validate_string,
    check_sql_suspicious,
    escape_html,
    require_rest_api_auth,
)

log = logging.getLogger(__name__)


import hashlib
import hmac


def _check_auth(request: web.Request) -> bool:
    """Ключ не настроен — доступа нет.

    Раньше первой строкой стояло «ADMIN_SECRET is None — пускаем всех». Сегодня
    config подставляет пустую строку и до этой ветки дело не доходит, но она
    оставалась заряженной: убери значение по умолчанию в config.py — и весь
    REST API (отправка сообщений ОТ ИМЕНИ аккаунта, чтение переписки, список
    аккаунтов) молча становится публичным.
    """
    if not ADMIN_SECRET:
        log.warning("rest_api: ADMIN_SECRET не задан — все запросы отклоняются")
        return False
    key = (
        request.headers.get("X-Api-Key", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    # Сравниваем байты: compare_digest на строках падает TypeError, если в ключе
    # есть не-ASCII. Ключ приходит снаружи — падение превратило бы отказ 401 в
    # 500 с трейсом, то есть в способ отличать «не тот ключ» от «сломались».
    return hmac.compare_digest(key.encode("utf-8"), ADMIN_SECRET.encode("utf-8"))


def _unauth() -> web.Response:
    return web.json_response({"error": "Unauthorized"}, status=401)


def _presented_key(request: web.Request) -> str:
    return (
        request.headers.get("X-Api-Key", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )


async def _authorize(request: web.Request, pool, scope: str = "read") -> dict | None:
    """Кто зовёт: {"admin": bool, "owner_id": int|None, "scopes": [...], "key_id"}.

    Сначала служебный ADMIN_SECRET (`_check_auth(request)`), затем личный ключ
    владельца. None — отказ (ключа нет, он отозван, или база недоступна).
    Область проверяет `_scope_denied`, а не эта функция: 401 и 403 — разные
    ответы, интеграции важно отличать «ключ не тот» от «ключу нельзя».
    """
    if _check_auth(request):
        return {"admin": True, "owner_id": None, "scopes": ["read", "write"],
                "key_id": None}
    from services import api_keys as _keys
    info = await _keys.authenticate(pool, _presented_key(request))
    if not info:
        return None
    return {"admin": False, "owner_id": info["owner_id"],
            "scopes": info["scopes"], "key_id": info["key_id"]}


def _scope_denied(ctx: dict, scope: str) -> web.Response | None:
    if scope in (ctx.get("scopes") or []):
        return None
    return web.json_response(
        {"error": f"У ключа нет области «{scope}» — выпустите ключ с нужным доступом"},
        status=403)


def resolve_owner(ctx: dict, supplied) -> tuple[int | None, str | None]:
    """owner_id для запроса: (owner_id, None) или (None, текст отказа).

    Личный ключ: владелец — из ключа; переданный owner_id обязан совпасть
    (иначе ключ одного владельца читал бы аккаунты другого — ровно то, чем
    был опасен общий ADMIN_SECRET). Служебный ключ: owner_id обязателен.
    Чистая функция — тестируема.
    """
    sup = validate_integer(supplied, min_val=1) if supplied not in (None, "") else None
    if supplied not in (None, "") and sup is None:
        return None, "owner_id должен быть положительным числом"
    if ctx.get("admin"):
        if sup is None:
            return None, "owner_id required"
        return sup, None
    own = int(ctx["owner_id"])
    if sup is not None and sup != own:
        return None, "forbidden_owner"
    return own, None


def _owner_or_error(ctx: dict, supplied) -> tuple[int | None, web.Response | None]:
    owner, err = resolve_owner(ctx, supplied)
    if err == "forbidden_owner":
        return None, web.json_response(
            {"error": "Ключ принадлежит другому владельцу"}, status=403)
    if err:
        return None, _bad(err)
    return owner, None


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
    pool: "asyncpg.Pool | None" = None,
) -> list[dict]:
    """Получить последние сообщения из чата (включая ЛС с ботом).

    `pool` нужен, чтобы назначенную Telegram паузу записать в flood_engine:
    без неё FloodWait оставался только в логе, а аккаунт выглядел отдохнувшим
    для следующей операции — и шёл за новым штрафом."""
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
            if pool is not None and (_acc or {}).get("id"):
                try:
                    from services.flood_engine import record_flood
                    await record_flood(pool, int(_acc["id"]),
                                       int(getattr(e, "seconds", 0) or 0),
                                       "read_messages")
                except Exception:
                    log_exc_swallow(log, "rest_api: запись флуд-паузы не сработала")
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


def _iso(v):
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else v


def operation_public(r) -> dict:
    """Строка operation_queue → публичный вид для API. Чистая функция."""
    from services import operation_bus as _bus
    return {
        "id": r["id"],
        "type": r["op_type"],
        "type_label": _bus.describe(r["op_type"]),
        "label": r.get("label") or "",
        "status": r["status"],
        "total_items": r.get("total_items") or 0,
        "done_items": r.get("done_items") or 0,
        "error": r.get("error_msg") or None,
        "created_at": _iso(r.get("created_at")),
        "started_at": _iso(r.get("started_at")),
        "finished_at": _iso(r.get("finished_at")),
    }


_OP_STATUSES = ("pending", "running", "paused", "scheduled", "waiting_approval",
                "done", "partial", "failed", "cancelled")


def add_routes(app: web.Application, pool: asyncpg.Pool, bot: Bot) -> None:
    """Добавить REST API маршруты к существующему aiohttp-приложению."""

    async def _gate(request: web.Request, scope: str, max_requests: int):
        """Общая калитка ручки: ключ → область → лимит. (ctx, None) | (None, ответ)."""
        ctx = await _authorize(request, pool, scope)
        if ctx is None:
            return None, _unauth()
        denied = _scope_denied(ctx, scope)
        if denied is not None:
            return None, denied
        # Лимит — на владельца ключа, а не на IP: n8n с одного адреса может
        # обслуживать нескольких владельцев, а один владелец — ходить с разных.
        if not await check_rate_limit(request, uid=ctx["owner_id"],
                                      max_requests=max_requests, window=60):
            return None, rate_limit_response(request, ctx["owner_id"])
        return ctx, None

    async def api_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "Infragram REST API v1"})

    async def api_me(request: web.Request) -> web.Response:
        ctx, err = await _gate(request, "read", 60)
        if err is not None:
            return err
        return web.json_response({
            "owner_id": ctx["owner_id"],
            "admin": bool(ctx["admin"]),
            "scopes": ctx["scopes"],
            "key_id": ctx["key_id"],
        })

    async def api_accounts(request: web.Request) -> web.Response:
        ctx, err = await _gate(request, "read", 30)
        if err is not None:
            return err
        owner_id, err = _owner_or_error(ctx, request.query.get("owner_id"))
        if err is not None:
            return err
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

    async def api_operations(request: web.Request) -> web.Response:
        ctx, err = await _gate(request, "read", 60)
        if err is not None:
            return err
        owner_id, err = _owner_or_error(ctx, request.query.get("owner_id"))
        if err is not None:
            return err
        status = (request.query.get("status") or "").strip().lower() or None
        if status is not None and status not in _OP_STATUSES:
            return _bad("unknown status; one of: " + ", ".join(_OP_STATUSES))
        limit = validate_integer(request.query.get("limit", "50"), min_val=1,
                                 max_val=500) or 50
        before = validate_integer(request.query.get("before_id"), min_val=1)
        try:
            rows = await pool.fetch(
                """SELECT id, op_type, label, status, total_items, done_items,
                          error_msg, created_at, started_at, finished_at
                   FROM operation_queue
                   WHERE owner_id=$1
                     AND ($2::text IS NULL OR status=$2)
                     AND ($3::bigint IS NULL OR id < $3)
                   ORDER BY id DESC LIMIT $4""",
                owner_id, status, before, limit)
        except Exception:
            log_exc_swallow(log, "rest_api: api_operations error")
            return web.json_response({"error": "internal error"}, status=500)
        items = [operation_public(dict(r)) for r in rows]
        return web.json_response({
            "operations": items,
            "next_before_id": items[-1]["id"] if len(items) == limit else None,
        })

    async def api_operation(request: web.Request) -> web.Response:
        ctx, err = await _gate(request, "read", 120)
        if err is not None:
            return err
        owner_id, err = _owner_or_error(ctx, request.query.get("owner_id"))
        if err is not None:
            return err
        op_id = validate_integer(request.match_info.get("op_id"), min_val=1)
        if not op_id:
            return _bad("op_id must be a positive integer")
        try:
            row = await pool.fetchrow(
                """SELECT id, op_type, label, status, total_items, done_items,
                          error_msg, created_at, started_at, finished_at
                   FROM operation_queue WHERE id=$1 AND owner_id=$2""",
                op_id, owner_id)
        except Exception:
            log_exc_swallow(log, "rest_api: api_operation error")
            return web.json_response({"error": "internal error"}, status=500)
        if not row:
            return web.json_response({"error": "operation not found"}, status=404)
        return web.json_response(operation_public(dict(row)))

    async def api_send_message(request: web.Request) -> web.Response:
        ctx, err = await _gate(request, "write", 10)
        if err is not None:
            return err
        try:
            data = await request.json()
        except Exception:
            return _bad("invalid JSON")
        if not isinstance(data, dict):
            return _bad("invalid JSON")
        owner_id, err = _owner_or_error(ctx, data.get("owner_id"))
        if err is not None:
            return err
        account_id = validate_integer(data.get("account_id"), min_val=1)
        chat_id = validate_integer(data.get("chat_id"))
        text = validate_string(data.get("text"), max_len=4096)
        if not all([owner_id, account_id, chat_id, text]):
            return _bad("account_id, chat_id, text are required")
        # SQL injection defense: check text for suspicious patterns
        if check_sql_suspicious(text):
            return _bad("Invalid characters in text")
        try:
            acc = await db.get_account_for_telethon(
                pool, account_id, owner_id
            )
            if not acc:
                return web.json_response({"error": "account not found"}, status=404)
            ok = await account_manager.send_message_via_account(
                acc["session_str"], chat_id, text, _acc=dict(acc)
            )
            return web.json_response({"ok": ok})
        except Exception:
            log_exc_swallow(log, "rest_api: api_send_message error")
            return web.json_response({"error": "internal error"}, status=500)

    async def api_click_button(request: web.Request) -> web.Response:
        ctx, err = await _gate(request, "write", 10)
        if err is not None:
            return err
        try:
            data = await request.json()
        except Exception:
            return _bad("invalid JSON")
        if not isinstance(data, dict):
            return _bad("invalid JSON")
        owner_id, err = _owner_or_error(ctx, data.get("owner_id"))
        if err is not None:
            return err
        account_id = validate_integer(data.get("account_id"), min_val=1)
        chat_id = validate_integer(data.get("chat_id"))
        message_id = validate_integer(data.get("message_id"), min_val=1)
        button_data = validate_string(data.get("button_data"), max_len=512)
        if not all([owner_id, account_id, chat_id, message_id, button_data]):
            return _bad(
                "account_id, chat_id, message_id, button_data are required"
            )
        try:
            acc = await db.get_account_for_telethon(
                pool, account_id, owner_id
            )
            if not acc:
                return web.json_response({"error": "account not found"}, status=404)
            result = await _click_inline_button(
                acc["session_str"],
                chat_id,
                message_id,
                button_data,
                _acc=dict(acc),
            )
            return web.json_response(result)
        except Exception:
            log_exc_swallow(log, "rest_api: api_click_button error")
            return web.json_response({"error": "internal error"}, status=500)

    async def api_get_messages(request: web.Request) -> web.Response:
        ctx, err = await _gate(request, "read", 15)
        if err is not None:
            return err
        owner_id, err = _owner_or_error(ctx, request.query.get("owner_id"))
        if err is not None:
            return err
        account_id = validate_integer(request.query.get("account_id"), min_val=1)
        chat_id = validate_integer(request.query.get("chat_id"))
        if not all([owner_id, account_id, chat_id]):
            return _bad("account_id, chat_id are required")
        limit = min(validate_integer(request.query.get("limit", "20"), min_val=1, max_val=100) or 20, 100)
        try:
            acc = await db.get_account_for_telethon(pool, account_id, owner_id)
            if not acc:
                return web.json_response({"error": "account not found"}, status=404)
            messages = await _get_chat_messages(
                acc["session_str"], chat_id, limit=limit, _acc=dict(acc),
                pool=pool,
            )
            return web.json_response({"messages": messages, "count": len(messages)})
        except Exception:
            log_exc_swallow(log, "rest_api: api_get_messages error")
            return web.json_response({"error": "internal error"}, status=500)

    app.router.add_get("/api/v1/health", api_health)
    app.router.add_get("/api/v1/me", api_me)
    app.router.add_get("/api/v1/accounts", api_accounts)
    app.router.add_get("/api/v1/operations", api_operations)
    app.router.add_get("/api/v1/operations/{op_id}", api_operation)
    app.router.add_post("/api/v1/send_message", api_send_message)
    app.router.add_post("/api/v1/click_button", api_click_button)
    app.router.add_get("/api/v1/get_messages", api_get_messages)

    log.info(
        "REST API routes registered: /api/v1/{health,me,accounts,operations,"
        "send_message,click_button,get_messages}"
    )

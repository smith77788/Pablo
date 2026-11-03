"""«Мои данные» в мини-аппе: выгрузить архивом и удалить по запросу.

ЗАЧЕМ. Выгрузка и удаление данных владельца (152-ФЗ, GDPR) были только
командами бота — /my_data и /delete_my_data. В «Настройках» мини-аппа, где
человек такие вещи и ищет, их не было вовсе. Логика одна —
services/owner_data.py; здесь только двери к ней.

Удаление необратимо, поэтому сервер требует то же слово-подтверждение, что и
бот: случайный запрос или двойное касание ничего не сотрут.
"""
from __future__ import annotations

from aiohttp import web

CONFIRM_WORD = "ПОДТВЕРЖДАЮ"


def setup_routes(app: web.Application, pool) -> None:
    from services import owner_data
    from services.mini_app_api import (
        _INTERNAL_ERROR, _content_disposition, _err, _get_uid, _json_resp,
        bad_body_response, log,
    )

    async def my_data_export(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            blob, counts = await owner_data.export_owner_data(pool, uid)
        except Exception:
            log.exception("my_data_export uid=%s", uid)
            return _err(_INTERNAL_ERROR, 500)
        return web.Response(
            body=blob, content_type="application/zip",
            headers={"Content-Disposition": _content_disposition(f"infragram_my_data_{uid}.zip")})

    async def my_data_delete(request: web.Request) -> web.Response:
        uid = _get_uid(request)
        if not uid:
            return _err("Unauthorized", 401)
        try:
            data = await request.json()
        except Exception as exc:
            bad = bad_body_response(request, exc)
            return bad if bad is not None else _err("Не удалось разобрать запрос", 400)
        if str((data or {}).get("confirm") or "").strip() != CONFIRM_WORD:
            return _err(f"Чтобы удалить данные, введите слово {CONFIRM_WORD}", 400)
        try:
            res = await owner_data.delete_owner_data(pool, uid)
        except owner_data.OwnerDataDeleteError as exc:
            log.error("my_data_delete: не удалено uid=%s: %s", uid, exc)
            return _err("Удалить не получилось, ничего не изменено. Повторите позже "
                        "или напишите в поддержку.", 409)
        except Exception:
            log.exception("my_data_delete uid=%s", uid)
            return _err("Удалить не получилось, ничего не изменено.", 500)
        rows = sum((res.get("deleted") or {}).values())
        return _json_resp({"ok": True, "deleted_rows": int(rows)})

    app.router.add_get("/api/miniapp/my_data/export", my_data_export)
    app.router.add_post("/api/miniapp/my_data/delete", my_data_delete)

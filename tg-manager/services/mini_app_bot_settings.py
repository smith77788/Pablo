"""Owner-scoped Bot API 10.5 settings; main runtime configuration is untouched."""
from __future__ import annotations

import aiohttp
from aiohttp import web

from services import bot_settings


def setup_routes(app: web.Application, pool) -> None:
    from services.mini_app_api import _bot_token, _err, _get_uid, _json_resp, log

    async def _owned_bot(request):
        uid = _get_uid(request)
        if not uid:
            return None, _err("Unauthorized", 401)
        try:
            bot_id = int(request.match_info["bot_id"])
            if bot_id <= 0 or bot_id > 2**52:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return None, _err("Неверный номер бота", 400)
        row = await pool.fetchrow(
            """SELECT bot_id, token FROM managed_bots
               WHERE bot_id=$1 AND added_by=$2 AND deleted_at IS NULL AND is_active""",
            bot_id, uid)
        if not row:
            return None, _err("Бот не найден", 404)
        return row, None

    async def _callback_mode_protected(request, row) -> bool:
        # The main manager and personal mirrors route callbacks through handlers
        # that require message context; arbitrary owned bots use the raw runtime.
        try:
            manager_id = int(_bot_token().split(":", 1)[0])
        except (ValueError, AttributeError):
            manager_id = None
        if row["bot_id"] == manager_id:
            return True
        return bool(await pool.fetchval(
            "SELECT 1 FROM mirror_bots WHERE bot_id=$1 AND owner_id=$2",
            row["bot_id"], _get_uid(request)))

    def _telegram_error(exc):
        payload = {"error": str(exc)}
        if exc.retry_after is not None:
            payload["retry_after"] = exc.retry_after
        response = _json_resp(payload, status=exc.status)
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    async def bot_api_settings_get(request: web.Request) -> web.Response:
        try:
            row, error = await _owned_bot(request)
            if error is not None:
                return error
            async with aiohttp.ClientSession() as session:
                result = await bot_settings.read_settings(session, row["token"], row["bot_id"])
            if await _callback_mode_protected(request, row):
                for field in result["fields"]:
                    if field["key"] == "callback_query_mode":
                        field["choices"] = ["default"]
                        field["description"] = "Боту управления нужен обычный формат нажатий кнопок."
            return _json_resp(result)
        except bot_settings.BotSettingsError as exc:
            return _telegram_error(exc)
        except Exception as exc:  # noqa: BLE001 — boundary must not expose bot credentials
            log.error("bot_api_settings_get: failed (%s)", type(exc).__name__)
            return _err("Не удалось загрузить настройки бота", 503)

    async def bot_api_settings_post(request: web.Request) -> web.Response:
        try:
            row, error = await _owned_bot(request)
            if error is not None:
                return error
            try:
                body = await request.json()
            except (ValueError, TypeError):
                return _err("Не удалось разобрать запрос", 400)
            try:
                if not isinstance(body, dict) or set(body) != {"settings"}:
                    raise ValueError("Передайте изменённые настройки бота")
                patch = bot_settings.validate_patch(body["settings"])
            except (ValueError, TypeError) as exc:
                return _err(str(exc) or "Неверный запрос", 400)
            if patch.get("callback_query_mode") in ("auto", "inline") and await _callback_mode_protected(request, row):
                return _err("Для бота управления доступен только обычный формат нажатий кнопок", 400)
            async with aiohttp.ClientSession() as session:
                result = await bot_settings.update_settings(session, row["token"], row["bot_id"], patch)
            return _json_resp(result)
        except bot_settings.BotSettingsError as exc:
            return _telegram_error(exc)
        except Exception as exc:  # noqa: BLE001 — boundary must not expose bot credentials
            log.error("bot_api_settings_post: failed (%s)", type(exc).__name__)
            return _err("Не удалось изменить настройки бота", 503)

    app.router.add_get("/api/miniapp/bot/{bot_id}/settings", bot_api_settings_get)
    app.router.add_post("/api/miniapp/bot/{bot_id}/settings", bot_api_settings_post)

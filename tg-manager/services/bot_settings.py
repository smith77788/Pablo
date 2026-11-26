"""Bot API 10.5 settings: strict patches and public BotFullInfo projection.

Official contract: https://core.telegram.org/bots/api#setmysettings
Only explicitly supplied settings are changed. Unknown readback values stay null.
"""
from __future__ import annotations

import re

from services import bot_api

API_VERSION = "10.5"
FIELDS = (
    {"key": "supports_inline_queries", "label": "Встроенный поиск через @имя бота", "type": "boolean"},
    {"key": "inline_query_placeholder", "label": "Подсказка встроенного поиска", "type": "string"},
    {"key": "needs_inline_query_location", "label": "Запрашивать геопозицию при встроенном поиске", "type": "boolean"},
    {"key": "inline_feedback_share_denominator", "label": "Получать результаты встроенного поиска", "type": "integer", "choices": [0, 1, 10, 100, 1000]},
    {"key": "can_join_groups", "label": "Разрешить добавление бота в группы", "type": "boolean"},
    {"key": "can_read_all_group_messages", "label": "Читать все сообщения группы (выключить режим приватности)", "type": "boolean"},
    {"key": "can_connect_to_business", "label": "Подключение к бизнес-аккаунтам", "type": "boolean"},
    {"key": "can_manage_bots", "label": "Создание и управление другими ботами", "type": "boolean"},
    {"key": "supports_guest_queries", "label": "Гостевые обращения из других чатов", "type": "boolean"},
    {"key": "supports_join_request_queries", "label": "Обработка заявок на вступление", "type": "boolean"},
    {"key": "has_topics_enabled", "label": "Темы в личном чате с ботом", "type": "boolean"},
    {"key": "allows_users_to_create_topics", "label": "Пользователи могут создавать и удалять темы", "type": "boolean"},
    {"key": "has_topic_sidebar", "label": "Показывать темы в боковой панели", "type": "boolean"},
    {"key": "can_communicate_with_bots", "label": "Общение с другими ботами", "type": "boolean"},
    {"key": "callback_query_mode", "label": "Формат нажатий кнопок", "type": "enum", "choices": ["default", "auto", "inline"]},
)
_USERNAME = re.compile(r"[A-Za-z0-9_]{1,64}\Z")


class BotSettingsError(Exception):
    """A public Russian error, with no transport response or credentials."""

    def __init__(self, message: str, status: int = 502, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def validate_patch(raw) -> dict:
    """Reject malformed/unknown fields; preserve false, zero and empty text."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Выберите хотя бы одну настройку")
    fields = {field["key"]: field for field in FIELDS}
    if any(key not in fields for key in raw):
        raise ValueError("Передана неизвестная настройка бота")
    patch = {}
    for key, value in raw.items():
        field = fields[key]
        kind = field["type"]
        if kind == "boolean":
            valid = type(value) is bool
        elif kind == "string":
            valid = isinstance(value, str) and len(value) <= 1024
        elif kind == "integer":
            valid = type(value) is int and value in field["choices"]
        else:
            valid = isinstance(value, str) and value in field["choices"]
        if not valid:
            raise ValueError(f"Неверное значение: {field['label']}")
        patch[key] = value
    return patch


def public_info(info: dict) -> dict:
    """GetMe now returns BotFullInfo, not a User: expose only known public data."""
    username = info.get("username")
    username = username if isinstance(username, str) and _USERNAME.fullmatch(username) else None
    aliases = info.get("active_usernames")
    aliases = [item for item in aliases if isinstance(item, str) and _USERNAME.fullmatch(item)] if isinstance(aliases, list) else []
    settings = {}
    for field in FIELDS:
        value = info.get(field["key"])
        if field["type"] == "boolean":
            settings[field["key"]] = value if type(value) is bool else None
        elif field["type"] == "string":
            settings[field["key"]] = value if isinstance(value, str) else None
        else:
            # Telegram does not currently return feedback denominator/callback mode.
            settings[field["key"]] = None
    return {
        "api_version": API_VERSION,
        "bot": {"id": info["id"], "first_name": info.get("first_name") or "", "username": username,
                "active_usernames": aliases, "has_main_web_app": info.get("has_main_web_app") if type(info.get("has_main_web_app")) is bool else None},
        "settings": settings,
        "fields": [{**field, **({"choices": list(field["choices"])} if "choices" in field else {})} for field in FIELDS],
        "invite_link": f"https://t.me/botinvite/{username}" if username else None,
    }


def _result(response: dict):
    if response.get("ok") is True:
        return response.get("result")
    retry = bot_api.retry_after_of(response)
    code = response.get("error_code")
    if code == 429:
        raise BotSettingsError("Telegram ограничил частоту запросов. Повторите позже.", 429, retry)
    if code in (401, 404):
        # An upstream bot-token problem is not an expired owner login (401).
        raise BotSettingsError("Токен бота недействителен. Переподключите бота.", 409)
    if code == 403:
        raise BotSettingsError("Telegram запретил изменение настроек этого бота.", 409)
    if code == 400:
        raise BotSettingsError("Telegram отклонил настройки. Проверьте значения и доступность функции.", 400)
    raise BotSettingsError("Не удалось получить ответ Telegram. Повторите позже.", 503)


async def read_settings(session, token: str, bot_id: int) -> dict:
    result = _result(await bot_api._call(session, token, "getMe"))
    if not isinstance(result, dict) or type(result.get("id")) is not int or result.get("is_bot") is not True:
        raise BotSettingsError("Telegram вернул некорректные сведения о боте")
    if result["id"] != bot_id:
        raise BotSettingsError("Токен принадлежит другому боту. Переподключите бота.", 409)
    return public_info(result)


async def update_settings(session, token: str, bot_id: int, raw) -> dict:
    patch = validate_patch(raw)
    await read_settings(session, token, bot_id)
    result = _result(await bot_api._call(session, token, "setMySettings", **patch))
    if result is not True:
        raise BotSettingsError("Telegram не подтвердил изменение настроек")
    return {"ok": True, "applied": patch}

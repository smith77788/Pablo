"""Бот-зеркало владельца: личный запасной вход в панель управления.

Если основной бот Infragram забанят или ограничат, владелец теряет доступ к
панели. Зеркало — его собственный бот (токен из @BotFather) с той же кнопкой
меню, открывающей наш мини-апп. Ведётся БЕЗ отдельного aiogram-инстанса:
достаточно один раз выставить боту web-app кнопку меню на MINI_APP_URL?m=<id>.

Безопасность входа через зеркало (см. mini_app_api.auth): initData, пришедший
из зеркала, подписан ТОКЕНОМ ЗЕРКАЛА — проверяем его этим токеном, но сессию
выдаём ОСНОВНЫМ токеном, поэтому остальные ручки (их сотни) не меняются и не
знают о зеркалах. Токен зеркала нигде наружу не отдаётся.
"""
from __future__ import annotations

import logging

from config import MINI_APP_URL
from services import bot_api
from services.token_vault import decrypt_token, encrypt_plaintext_token

log = logging.getLogger(__name__)

_MENU_TEXT = "🚀 Открыть панель"
# Те же команды, что ставит основной бот новичку: зеркало отвечает на /start.
_COMMANDS = [{"command": "start", "description": "Открыть панель управления"}]


class MirrorError(RuntimeError):
    """Причина отказа — по-русски, для владельца."""


def _main_bot_id() -> int:
    """id основного бота из его токена (<bot_id>:<hash>)."""
    import os
    tok = os.getenv("BOT_TOKEN", os.getenv("MANAGER_BOT_TOKEN", ""))
    try:
        return int(tok.split(":", 1)[0])
    except (ValueError, AttributeError, IndexError):
        return 0


def menu_url(mirror_id: int) -> str:
    """Адрес мини-аппа для кнопки меню зеркала: по ?m=<id> /auth знает, чьим
    токеном проверять initData."""
    base = (MINI_APP_URL or "").rstrip("/")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}m={int(mirror_id)}"


async def register(pool, owner_id: int, token: str, http) -> dict:
    """Подключить/обновить зеркало владельца. Проверяет токен через getMe,
    запрещает основной бот, шифрует и сохраняет, выставляет кнопку меню и
    команды. Возвращает {bot_id, username, first_name}. Бросает MirrorError."""
    token = (token or "").strip()
    # Токен @BotFather: <digits>:<35+ base64-символов>. Грубая проверка формы —
    # чтобы не слать в Telegram заведомый мусор.
    if ":" not in token or not token.split(":", 1)[0].isdigit() or len(token) < 40:
        raise MirrorError("Это не похоже на токен бота. Скопируйте его у @BotFather целиком.")
    me = await bot_api.get_me(http, token)
    if not me or not me.get("id"):
        raise MirrorError("Telegram не принял токен. Проверьте, что скопировали его верно и бот не удалён.")
    bot_id = int(me["id"])
    if bot_id == _main_bot_id():
        raise MirrorError("Это токен основного бота. Зеркало — ОТДЕЛЬНЫЙ бот: создайте нового у @BotFather.")
    username = (me.get("username") or "").lstrip("@")
    first_name = me.get("first_name") or ""
    enc = encrypt_plaintext_token(token)
    try:
        row = await pool.fetchrow(
            """INSERT INTO mirror_bots(owner_id, bot_id, username, first_name, token_enc)
               VALUES($1,$2,$3,$4,$5)
               ON CONFLICT (owner_id) DO UPDATE
                 SET bot_id=EXCLUDED.bot_id, username=EXCLUDED.username,
                     first_name=EXCLUDED.first_name, token_enc=EXCLUDED.token_enc,
                     updated_at=now()
               RETURNING id""",
            owner_id, bot_id, username, first_name, enc)
    except Exception as exc:
        log.warning("mirror_bot.register owner=%s: %s", owner_id, exc)
        raise MirrorError("Не удалось сохранить зеркало — попробуйте ещё раз.")
    mirror_id = int(row["id"])
    # Кнопка меню и команды — чтобы зеркало СРАЗУ открывало панель. Сбой здесь не
    # критичен для записи (её можно переставить повторным подключением), но без
    # кнопки зеркало бесполезно, поэтому о сбое сообщаем.
    menu_ok = True
    try:
        res = await bot_api.set_chat_menu_button(
            http, token, text=_MENU_TEXT, web_app_url=menu_url(mirror_id))
        menu_ok = bool(res.get("ok"))
    except Exception as exc:
        log.warning("mirror_bot.register menu owner=%s: %s", owner_id, exc)
        menu_ok = False
    try:
        await bot_api.set_my_commands(http, token, _COMMANDS)
    except Exception as exc:
        log.debug("mirror_bot.register commands owner=%s: %s", owner_id, exc)
    return {"id": mirror_id, "bot_id": bot_id, "username": username,
            "first_name": first_name, "menu_ok": menu_ok}


async def get_token_by_id(pool, mirror_id: int) -> str | None:
    """Токен зеркала по id — ТОЛЬКО для проверки initData в /auth. Наружу не
    отдаётся. None, если такого зеркала нет."""
    try:
        enc = await pool.fetchval(
            "SELECT token_enc FROM mirror_bots WHERE id=$1", int(mirror_id))
    except Exception as exc:
        log.debug("mirror_bot.get_token_by_id %s: %s", mirror_id, exc)
        return None
    if not enc:
        return None
    try:
        return decrypt_token(enc)
    except Exception as exc:
        log.warning("mirror_bot.get_token_by_id decrypt %s: %s", mirror_id, exc)
        return None


async def find_user_by_init_data(pool, init_data: str, validate) -> dict | None:
    """Найти зеркало, чьим токеном подтверждается initData, и вернуть его
    пользователя. Нужен для web-app кнопок, которые хендлеры шлют из зеркала БЕЗ
    ?m=<id> в адресе: там узнать зеркало можно только перебором. Запускается
    только когда основной токен подпись НЕ подтвердил, то есть редко.
    """
    try:
        rows = await pool.fetch(
            "SELECT token_enc FROM mirror_bots ORDER BY id LIMIT 500")
    except Exception as exc:
        log.debug("mirror_bot.find_user_by_init_data: %s", exc)
        return None
    for r in rows:
        try:
            tok = decrypt_token(r["token_enc"])
        except Exception:
            continue
        user = validate(init_data, tok)
        if user:
            return user
    return None


async def get_for(pool, owner_id: int) -> dict | None:
    """Зеркало владельца БЕЗ токена — для экрана. None, если нет."""
    try:
        row = await pool.fetchrow(
            "SELECT id, bot_id, username, first_name, created_at, updated_at "
            "FROM mirror_bots WHERE owner_id=$1", owner_id)
    except Exception as exc:
        log.debug("mirror_bot.get_for owner=%s: %s", owner_id, exc)
        return None
    return dict(row) if row else None


async def remove(pool, owner_id: int, http) -> bool:
    """Отключить зеркало: сбросить кнопку меню бота и удалить запись.
    Удаление из нашей базы — главное; сброс кнопки best-effort."""
    row = await pool.fetchrow(
        "SELECT token_enc FROM mirror_bots WHERE owner_id=$1", owner_id)
    if not row:
        return False
    try:
        token = decrypt_token(row["token_enc"])
        await bot_api.set_chat_menu_button(http, token, web_app_url=None)
    except Exception as exc:
        log.debug("mirror_bot.remove menu owner=%s: %s", owner_id, exc)
    try:
        await pool.execute("DELETE FROM mirror_bots WHERE owner_id=$1", owner_id)
    except Exception as exc:
        log.warning("mirror_bot.remove owner=%s: %s", owner_id, exc)
        return False
    return True

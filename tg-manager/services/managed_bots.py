"""Manager Mode (Telegram «Managed Bots», Bot API 9.6).

Наш бот-менеджер создаёт дочерних ботов и получает над ними полный контроль —
без ручного «BotFather → скопируй токен → вставь токен». Поток:

  1. Оператор задаёт желаемые @username и имя → мы строим ссылку
     t.me/newbot/<manager>/<username>?name=<name>.
  2. Оператор открывает ссылку → Telegram показывает форму создания бота
     (поля можно поправить) → подтверждает.
  3. Наш бот получает апдейт `managed_bot` (ManagedBotUpdated: user, bot_user).
  4. bot.get_managed_bot_token(user_id) → токен дочернего бота.
  5. Валидируем токен (getMe) и подключаем бота в нашу базу (managed_bots).

Требование (разово, вручную у оператора): включить «Bot Management Mode» у
@BotFather в его мини-аппе — иначе шаг 4 вернёт ошибку прав.

Здесь — чистая логика (сборка ссылки, валидация username, сохранение токена).
Сетевые вызовы Telegram (get_managed_bot_token) делает роутер; сюда передаётся
уже готовый токен, поэтому логику легко проверить без реального Telegram.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

import aiohttp
import asyncpg

log = logging.getLogger(__name__)

# Username бота: 5–32 символа, начинается с буквы, [A-Za-z0-9_], заканчивается на
# «bot» (правило Telegram). Проверяем заранее, чтобы не гнать заведомо плохую
# ссылку и подсказать оператору формат.
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*[Bb][Oo][Tt]$")


def validate_bot_username(username: str) -> tuple[bool, str]:
    """Проверить/нормализовать желаемый @username бота.

    Возвращает (True, normalized) либо (False, причина-ошибки для показа).
    """
    u = (username or "").strip().lstrip("@")
    if not u:
        return False, "пустой username"
    if len(u) < 5 or len(u) > 32:
        return False, "username должен быть 5–32 символа"
    if not u.lower().endswith("bot"):
        return False, "username должен заканчиваться на «bot»"
    if not _USERNAME_RE.match(u):
        return False, ("username может содержать только латиницу, цифры и «_», "
                       "начинаться с буквы")
    return True, u


def build_creation_link(manager_username: str, desired_username: str,
                        name: str = "") -> str:
    """Собрать deep-link создания бота через Manager Mode.

    Формат: https://t.me/newbot/<manager>/<desired_username>?name=<name>.
    manager_username — @username нашего бота-менеджера (без @).
    """
    mgr = (manager_username or "").lstrip("@")
    if not mgr:
        raise ValueError("не задан username бота-менеджера")
    ok, norm = validate_bot_username(desired_username)
    if not ok:
        raise ValueError(f"некорректный username: {norm}")
    link = f"https://t.me/newbot/{mgr}/{norm}"
    if name:
        link += f"?name={quote(name.strip())}"
    return link


async def store_managed_bot(pool: asyncpg.Pool, http: aiohttp.ClientSession,
                            token: str, creator_id: int,
                            expected_bot_id: int | None = None) -> dict:
    """Подключить дочернего бота по полученному токену.

    Валидирует токен через getMe, сверяет id с ожидаемым (из апдейта) и
    сохраняет в managed_bots (added_by = создатель). Возвращает:
      {"ok": True, "added": True|False|"taken", "username", "bot_id", "first_name"}
      {"ok": False, "reason": "invalid_token"|"id_mismatch"}
    """
    from services import bot_api
    from database import db

    if not token:
        return {"ok": False, "reason": "invalid_token"}
    info = await bot_api.get_me(http, token)
    if not info or not info.get("id"):
        return {"ok": False, "reason": "invalid_token"}
    bot_id = int(info["id"])
    # Санити: токен должен принадлежать тому боту, что назвал Telegram в апдейте.
    if expected_bot_id is not None and int(expected_bot_id) != bot_id:
        log.warning("managed_bots: id токена %s ≠ id из апдейта %s", bot_id, expected_bot_id)
        return {"ok": False, "reason": "id_mismatch"}
    username = info.get("username", "") or ""
    first_name = info.get("first_name", "") or ""
    added = await db.add_bot(
        pool, token=token, bot_id=bot_id, username=username,
        first_name=first_name, added_by=int(creator_id))
    # Пометим происхождение (для аналитики и того, что токеном мы владеем через
    # Managed API и можем его при необходимости заменить). Колонка — best-effort.
    if added is True:
        try:
            await pool.execute(
                "UPDATE managed_bots SET created_via='managed' WHERE bot_id=$1", bot_id)
        except Exception:
            log.debug("managed_bots: колонка created_via отсутствует — пропускаем")
    return {"ok": True, "added": added, "username": username,
            "bot_id": bot_id, "first_name": first_name}

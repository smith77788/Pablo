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
from urllib.parse import quote

import aiohttp
import asyncpg

log = logging.getLogger(__name__)

def validate_bot_username(username: str) -> tuple[bool, str]:
    """Проверить/нормализовать желаемый ОСНОВНОЙ @username создаваемого бота.

    Возвращает (True, normalized) либо (False, причина-ошибки для показа).
    Основной обязан оканчиваться на «bot» и сейчас; суффиксы app/ai/agent/game
    — это второй, дополнительный username (services/bot_usernames.py), и
    оператору, введшему такой, объясняем это вместо сухого «не тот формат».
    """
    from services import bot_usernames
    return bot_usernames.validate_main(username)


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


# ── Дополнительный @username с новым суффиксом (app/ai/agent/game) ───────────
#
# Установить его можно только у @BotFather (Premium у владельца бота): публичного
# метода Bot API нет. Поэтому мы не «ставим» username, а привязываем уже
# поставленный: проверяем формат, резолвим через аккаунт владельца, что он ведёт
# именно на этого бота, и храним. Резолв — best-effort: нет живого аккаунта,
# flood wait или сеть → сохраняем с verified=False и честно говорим об этом.

_RESOLVE_ACCOUNTS_MAX = 2


async def _resolve_username_id(pool: asyncpg.Pool, owner_id: int,
                               username: str) -> dict:
    """Резолв username'а через аккаунт(ы) владельца.

    {"status": "found", "user_id", "is_bot"} | {"status": "not_found"} |
    {"status": "unverified", "reason": "no_account"|"flood_wait"|"error"}
    """
    # Выбор аккаунтов — только через resource_selector: он знает про cooldown,
    # доверие и флуды. Сырой `ORDER BY id LIMIT 2` брал первые два активных
    # аккаунта подряд, в том числе сидящий в кулдауне после флуда, и резолв
    # имени уходил именно им — то есть ровно тем, кому сейчас нельзя.
    try:
        from services import resource_selector as _rsel
        accs = await _rsel.select_accounts(
            pool, owner_id, _RESOLVE_ACCOUNTS_MAX, action_type="resolve")
        accs = [a for a in accs if (a.get("session_str") or "").strip()]
    except Exception:
        log.exception("extra_username: выборка аккаунтов owner=%s", owner_id)
        return {"status": "unverified", "reason": "error"}
    if not accs:
        return {"status": "unverified", "reason": "no_account"}

    from services import account_manager
    from telethon.tl.functions.contacts import ResolveUsernameRequest
    from telethon.errors import (FloodWaitError, UsernameInvalidError,
                                 UsernameNotOccupiedError)
    import asyncio

    last_reason = "error"
    for acc in accs:
        client = None
        try:
            client = await account_manager.connect_client(
                acc["session_str"], dict(acc), "resolve", low_risk=True)
            res = await asyncio.wait_for(
                client(ResolveUsernameRequest(username=username)), timeout=20.0)
            users = getattr(res, "users", None) or []
            if not users:
                return {"status": "not_found"}
            u = users[0]
            return {"status": "found", "user_id": int(u.id),
                    "is_bot": bool(getattr(u, "bot", False))}
        except (UsernameNotOccupiedError, UsernameInvalidError):
            return {"status": "not_found"}
        except FloodWaitError as e:
            # Не обходим: этот аккаунт молчит, пробуем следующий, иначе — позже.
            log.info("extra_username: flood wait %ss на acc=%s",
                     getattr(e, "seconds", 0), acc["id"])
            last_reason = "flood_wait"
        except Exception as e:
            log.warning("extra_username: резолв @%s через acc=%s: %s",
                        username, acc["id"], str(e)[:120])
            last_reason = "error"
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    log.debug("extra_username: disconnect acc=%s", acc["id"])
    return {"status": "unverified", "reason": last_reason}


_UNVERIFIED_HINTS = {
    "no_account": "нет подключённого аккаунта для проверки",
    "flood_wait": "Telegram временно ограничил запросы, проверим позже",
    "error": "проверка не удалась, повторим позже",
}


async def set_extra_username(pool: asyncpg.Pool, owner_id: int, bot_id: int,
                             username: str | None, *, resolver=None) -> dict:
    """Привязать (или снять при пустом username) дополнительный username бота.

    Ответ: {"ok": True, "extra_username", "verified", "hint"} либо
    {"ok": False, "status": 400|404|409, "error"}. resolver — для тестов.
    """
    from services import bot_usernames

    row = await pool.fetchrow(
        "SELECT bot_id, username, extra_username FROM managed_bots "
        "WHERE bot_id=$1 AND added_by=$2", bot_id, owner_id)
    if not row:
        return {"ok": False, "status": 404, "error": "Бот не найден"}

    if not bot_usernames.normalize(username):
        await pool.execute(
            "UPDATE managed_bots SET extra_username=NULL, "
            "extra_username_verified=FALSE, extra_username_checked_at=NULL "
            "WHERE bot_id=$1 AND added_by=$2", bot_id, owner_id)
        log.info("extra_username: снят у bot=%s owner=%s", bot_id, owner_id)
        return {"ok": True, "extra_username": None, "verified": False, "hint": ""}

    ok, norm = bot_usernames.validate_extra(username)
    if not ok:
        return {"ok": False, "status": 400, "error": norm}
    if (row["username"] or "").lower() == norm.lower():
        return {"ok": False, "status": 400,
                "error": "это уже основной username бота"}

    taken = await pool.fetchval(
        "SELECT bot_id FROM managed_bots WHERE lower(extra_username)=lower($1) "
        "AND bot_id<>$2", norm, bot_id)
    if taken:
        return {"ok": False, "status": 409,
                "error": f"@{norm} уже привязан к другому боту"}

    res = await (resolver or _resolve_username_id)(pool, owner_id, norm)
    status = res.get("status")
    if status == "not_found":
        return {"ok": False, "status": 400, "error": (
            f"@{norm} пока не занят в Telegram. Сначала задайте его боту у "
            "@BotFather (нужен Telegram Premium), затем привяжите здесь")}
    if status == "found" and int(res.get("user_id") or 0) != int(bot_id):
        return {"ok": False, "status": 409, "error": (
            f"@{norm} принадлежит другому аккаунту или боту, не этому")}
    verified = status == "found"
    hint = "" if verified else _UNVERIFIED_HINTS.get(res.get("reason"), "")

    try:
        await pool.execute(
            "UPDATE managed_bots SET extra_username=$3, extra_username_verified=$4, "
            "extra_username_checked_at=NOW() WHERE bot_id=$1 AND added_by=$2",
            bot_id, owner_id, norm, verified)
    except asyncpg.UniqueViolationError:
        return {"ok": False, "status": 409,
                "error": f"@{norm} уже привязан к другому боту"}
    log.info("extra_username: bot=%s owner=%s @%s verified=%s",
             bot_id, owner_id, norm, verified)
    return {"ok": True, "extra_username": norm, "verified": verified, "hint": hint}

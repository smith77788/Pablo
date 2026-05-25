"""Background service: check search ranking for tracked keywords."""
from __future__ import annotations
import asyncio
import logging
from typing import Any
import asyncpg
from services.account_manager import search_in_telegram
from database import db

log = logging.getLogger(__name__)
_INTERVAL = 3600  # check every hour


async def run(pool: asyncpg.Pool) -> None:
    await asyncio.sleep(60)  # startup delay
    while True:
        try:
            await _check_all(pool)
        except Exception as e:
            log.exception("ranking_checker error: %s", e)
        await asyncio.sleep(_INTERVAL)


async def _get_least_used_account(pool: asyncpg.Pool, owner_id: int) -> asyncpg.Record | None:
    """Выбирает активный аккаунт владельца, давно не использовавшийся (last_used ASC).

    Это равномерно распределяет нагрузку между несколькими аккаунтами.
    """
    return await pool.fetchrow(
        "SELECT id, session_str FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=true "
        "ORDER BY last_used ASC NULLS FIRST LIMIT 1",
        owner_id,
    )


async def check_bot_keywords(
    pool: asyncpg.Pool,
    bot_id: int,
    owner_id: int,
) -> list[dict[str, Any]]:
    """Проверяет все активные ключевые слова конкретного бота прямо сейчас.

    Возвращает список словарей вида:
        {"keyword": str, "keyword_id": int, "position": int | None, "error": bool}
    """
    # Fetch bot username
    bot_row = await pool.fetchrow(
        "SELECT username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id, owner_id,
    )
    if not bot_row:
        log.warning("check_bot_keywords: бот %s не найден для owner %s", bot_id, owner_id)
        return []

    bot_username = (bot_row["username"] or "").lower().lstrip("@")

    # Fetch active account for this owner (least recently used)
    account = await _get_least_used_account(pool, owner_id)
    if not account:
        log.warning(
            "check_bot_keywords: нет активных аккаунтов у пользователя %s",
            owner_id,
        )
        return []

    # Fetch active keywords for this bot
    keywords = await pool.fetch(
        "SELECT id, keyword FROM tracked_keywords "
        "WHERE bot_id=$1 AND owner_id=$2 AND is_active=TRUE",
        bot_id, owner_id,
    )
    if not keywords:
        return []

    results: list[dict[str, Any]] = []
    for kw in keywords:
        try:
            search_results = await search_in_telegram(account["session_str"], kw["keyword"])
            position: int | None = None
            for r in search_results:
                if r.get("is_bot") and r.get("username", "").lower().lstrip("@") == bot_username:
                    position = r["position"]
                    break

            await pool.execute(
                "INSERT INTO search_rankings(keyword_id, bot_id, position) VALUES($1,$2,$3)",
                kw["id"], bot_id, position,
            )
            await db.update_tg_account_used(pool, account["id"])
            log.debug(
                "check_bot_keywords: keyword=%r bot=%r position=%s",
                kw["keyword"], bot_username, position,
            )
            results.append({"keyword": kw["keyword"], "keyword_id": kw["id"], "position": position, "error": False})
            await asyncio.sleep(2)  # rate limit
        except Exception as exc:
            log.warning(
                "check_bot_keywords: ошибка при проверке %r: %s",
                kw["keyword"], exc,
            )
            results.append({"keyword": kw["keyword"], "keyword_id": kw["id"], "position": None, "error": True})

    return results


async def _check_all(pool: asyncpg.Pool) -> None:
    keywords = await pool.fetch(
        "SELECT tk.id, tk.keyword, tk.bot_id, tk.owner_id, mb.username as bot_username "
        "FROM tracked_keywords tk "
        "JOIN managed_bots mb ON mb.bot_id = tk.bot_id "
        "WHERE tk.is_active = true"
    )
    if not keywords:
        log.debug("ranking_checker: нет активных ключевых слов для проверки")
        return

    log.info("ranking_checker: начинаю проверку %d ключевых слов", len(keywords))

    for kw in keywords:
        try:
            # Выбираем аккаунт владельца, который дольше всего не использовался
            account = await _get_least_used_account(pool, kw["owner_id"])
            if not account:
                log.warning(
                    "ranking_checker: нет активных аккаунтов у пользователя %s "
                    "для ключевого слова %r — пропускаем",
                    kw["owner_id"],
                    kw["keyword"],
                )
                continue

            results = await search_in_telegram(account["session_str"], kw["keyword"])
            bot_username = (kw["bot_username"] or "").lower().lstrip("@")
            position = None
            for r in results:
                if r["is_bot"] and r["username"].lower().lstrip("@") == bot_username:
                    position = r["position"]
                    break

            await pool.execute(
                "INSERT INTO search_rankings(keyword_id, bot_id, position) VALUES($1,$2,$3)",
                kw["id"], kw["bot_id"], position,
            )
            # Обновляем время последнего использования аккаунта
            await db.update_tg_account_used(pool, account["id"])
            log.debug(
                "ranking_checker: keyword=%r bot=%r position=%s account_id=%s",
                kw["keyword"], bot_username, position, account["id"],
            )
            await asyncio.sleep(2)  # rate limit between searches
        except Exception as e:
            log.warning(
                "ranking_checker: ошибка при проверке ключевого слова %r (keyword_id=%s): %s",
                kw["keyword"], kw["id"], e,
            )

"""Background service: check search ranking for tracked keywords."""
from __future__ import annotations
import asyncio
import logging
from typing import Any
import asyncpg
from aiogram import Bot
from services.account_manager import search_in_telegram
from database import db

log = logging.getLogger(__name__)
_INTERVAL = 3600  # check every hour

# Thresholds for notifications
_THRESHOLD_UP = 3    # improved by 3+ positions (lower number = better rank)
_THRESHOLD_DOWN = 5  # dropped by 5+ positions
_TOP_N = 20          # "top N" boundary for entry/exit notifications


async def run(pool: asyncpg.Pool, bot: Bot) -> None:
    await asyncio.sleep(60)  # startup delay
    while True:
        try:
            await _check_all(pool, bot)
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


async def _send_rank_notification(
    bot: Bot,
    owner_id: int,
    keyword: str,
    bot_username: str,
    old_position: int | None,
    new_position: int | None,
) -> None:
    """Sends a ranking change notification to the owner if thresholds are met.

    Rules:
    - Improved by _THRESHOLD_UP+ positions (lower number) → notify
    - Dropped by _THRESHOLD_DOWN+ positions (higher number) → notify
    - Entered top _TOP_N (old was None, new is not None) → notify
    - Fell out of top _TOP_N (old was not None, new is None) → notify
    """
    text: str | None = None
    bot_at = f"@{bot_username}" if bot_username else "бот"

    if old_position is None and new_position is not None:
        # Bot entered top-N
        text = (
            f"🎉 Бот {bot_at} появился в топ-{_TOP_N}!\n"
            f"Ключевое слово: «{keyword}» — позиция #{new_position}"
        )
    elif old_position is not None and new_position is None:
        # Bot fell out of top-N
        text = (
            f"⚠️ Бот {bot_at} выпал из топ-{_TOP_N}!\n"
            f"Ключевое слово: «{keyword}» — был #{old_position}"
        )
    elif old_position is not None and new_position is not None:
        diff = old_position - new_position  # positive = improved (lower rank number)
        if diff >= _THRESHOLD_UP:
            text = (
                f"📈 Позиция улучшилась: «{keyword}» — с #{old_position} на #{new_position} (бот {bot_at})"
            )
        elif -diff >= _THRESHOLD_DOWN:
            text = (
                f"📉 Позиция ухудшилась: «{keyword}» — с #{old_position} на #{new_position} (бот {bot_at})"
            )

    if text:
        try:
            await bot.send_message(owner_id, text)
            log.info(
                "_send_rank_notification: sent to owner=%s keyword=%r old=%s new=%s",
                owner_id, keyword, old_position, new_position,
            )
        except Exception as exc:
            log.warning(
                "_send_rank_notification: failed to send to owner=%s: %s",
                owner_id, exc,
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


async def _check_all(pool: asyncpg.Pool, bot: Bot) -> None:
    keywords = await pool.fetch(
        "SELECT tk.id, tk.keyword, tk.bot_id, tk.owner_id, tk.notify_enabled, "
        "mb.username as bot_username "
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

            # Fetch previous position before writing the new one
            prev_row = await db.get_latest_ranking(pool, kw["id"])
            old_position: int | None = prev_row["position"] if prev_row else None

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

            # Send notification if enabled and position changed significantly
            if kw["notify_enabled"]:
                await _send_rank_notification(
                    bot,
                    kw["owner_id"],
                    kw["keyword"],
                    kw["bot_username"] or "",
                    old_position,
                    position,
                )

            await asyncio.sleep(2)  # rate limit between searches
        except Exception as e:
            log.warning(
                "ranking_checker: ошибка при проверке ключевого слова %r (keyword_id=%s): %s",
                kw["keyword"], kw["id"], e,
            )

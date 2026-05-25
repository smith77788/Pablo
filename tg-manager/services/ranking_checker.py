"""Background service: check search ranking for tracked keywords."""
from __future__ import annotations
import asyncio
import logging
import asyncpg
from services.account_manager import search_in_telegram

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
            # Выбираем случайный активный аккаунт владельца бота
            account = await pool.fetchrow(
                "SELECT id, session_str FROM tg_accounts "
                "WHERE owner_id=$1 AND is_active=true "
                "ORDER BY RANDOM() LIMIT 1",
                kw["owner_id"],
            )
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
            log.debug(
                "ranking_checker: keyword=%r bot=%r position=%s",
                kw["keyword"], bot_username, position,
            )
            await asyncio.sleep(2)  # rate limit between searches
        except Exception as e:
            log.warning(
                "ranking_checker: ошибка при проверке ключевого слова %r (keyword_id=%s): %s",
                kw["keyword"], kw["id"], e,
            )

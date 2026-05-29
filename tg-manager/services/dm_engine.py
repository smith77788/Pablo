"""
DM Engine — отправка личных сообщений для DM-кампаний.

Поддерживает:
- Spintax {Привет|Здравствуйте|Добрый день}
- Humanized delays между отправками
- Классификацию ошибок (flood/blocked/deactivated/permission)
- Дедупликацию через dm_campaign_log
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import AsyncIterator

import asyncpg
from aiogram import Bot

log = logging.getLogger(__name__)


# ── Spintax ───────────────────────────────────────────────────────────────────

def expand_spintax(text: str) -> str:
    """Разворачивает {A|B|C} рекурсивно — каждый раз случайный вариант."""
    def _replace(m: re.Match) -> str:
        parts = m.group(1).split("|")
        return random.choice(parts)

    while "{" in text and "}" in text:
        text = re.sub(r"\{([^{}]+)\}", _replace, text)
    return text


# ── Error classification ──────────────────────────────────────────────────────

_SKIP_ERRORS = {
    "UserDeactivatedBan", "UserDeactivated",
    "UserNotMutualContact", "UserPrivacyRestricted",
    "InputUserDeactivated",
}
_FLOOD_ERRORS = {"FloodWaitError", "FloodWait"}
_BLOCKED_ERRORS = {
    "UserBlockedBan", "YouBlockedUser", "PeerFloodError",
    "ChatWriteForbidden", "UserBannedInChannel",
}


def _classify_error(exc: Exception) -> str:
    """Возвращает 'flood' | 'blocked' | 'skip' | 'auth' | 'retry'."""
    name = type(exc).__name__
    exc_str = str(exc)
    if name in _FLOOD_ERRORS or "FLOOD_WAIT" in exc_str.upper():
        return "flood"
    if name in _BLOCKED_ERRORS or "PEER_FLOOD" in exc_str:
        return "blocked"
    if name in _SKIP_ERRORS:
        return "skip"
    if "AUTH_KEY" in exc_str or "SESSION_REVOKED" in exc_str or "Unauthorized" in name:
        return "auth"
    return "retry"


def _extract_flood_seconds(exc: Exception) -> int:
    """Извлекает количество секунд флуд-вейта из исключения."""
    for attr in ("seconds", "x"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)):
            return int(val)
    m = re.search(r"(\d+)", str(exc))
    return int(m.group(1)) if m else 60


# ── Core send ─────────────────────────────────────────────────────────────────

async def send_dm(
    session_str: str,
    user_id: int,
    text: str,
    _acc: dict | None = None,
) -> dict:
    """
    Отправить одно личное сообщение.
    Возвращает {'status': 'sent'|'flood'|'blocked'|'skip'|'auth'|'retry', 'wait': int, 'error': str}
    """
    from services import account_manager
    try:
        await account_manager.send_message(session_str, user_id, text, _acc=_acc)
        return {"status": "sent"}
    except Exception as exc:
        kind = _classify_error(exc)
        wait = _extract_flood_seconds(exc) if kind == "flood" else 0
        return {"status": kind, "wait": wait, "error": str(exc)[:200]}


# ── Campaign runner ────────────────────────────────────────────────────────────

_MIN_DELAY = 8.0    # минимум секунд между отправками
_MAX_DELAY = 25.0   # максимум секунд между отправками
_FLOOD_PAUSE = 120  # пауза при flood (если нет явного wait)


async def _get_targets(pool: asyncpg.Pool, campaign: dict) -> list[int]:
    """Получить список tg_user_id для кампании."""
    campaign_id = campaign["id"]
    target_type = campaign["target_type"]
    target_id = campaign["target_id"]

    # Исключить уже отправленных
    sent_ids = {
        r["tg_user_id"]
        for r in await pool.fetch(
            "SELECT tg_user_id FROM dm_campaign_log WHERE campaign_id=$1 AND status='sent'",
            campaign_id,
        )
    }

    if target_type == "bot_users" and target_id:
        rows = await pool.fetch(
            "SELECT DISTINCT chat_id FROM bot_users WHERE bot_id=$1 AND chat_id > 0",
            target_id,
        )
        return [r["chat_id"] for r in rows if r["chat_id"] not in sent_ids]
    elif target_type == "crm":
        rows = await pool.fetch(
            "SELECT DISTINCT tg_user_id FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0",
            campaign["owner_id"],
        )
        return [r["tg_user_id"] for r in rows if r["tg_user_id"] not in sent_ids]
    return []


async def run_campaign(
    pool: asyncpg.Pool,
    bot: Bot,
    campaign_id: int,
) -> None:
    """Запустить или продолжить DM-кампанию. Вызывается из operation_queue."""
    campaign = await pool.fetchrow("SELECT * FROM dm_campaigns WHERE id=$1", campaign_id)
    if not campaign:
        log.error("dm_engine: campaign %d not found", campaign_id)
        return

    campaign = dict(campaign)
    owner_id = campaign["owner_id"]

    # Пометить как running
    await pool.execute(
        "UPDATE dm_campaigns SET status='running', started_at=COALESCE(started_at, now()) WHERE id=$1",
        campaign_id,
    )

    # Получить активные аккаунты владельца
    accounts = await pool.fetch(
        "SELECT a.id, a.session_str, a.phone, a.first_name, "
        "       a.device_model, a.system_version, a.app_version "
        "FROM tg_accounts a "
        "WHERE a.owner_id=$1 AND a.is_active=true "
        "ORDER BY a.trust_score DESC NULLS LAST",
        owner_id,
    )
    if not accounts:
        await pool.execute(
            "UPDATE dm_campaigns SET status='failed' WHERE id=$1", campaign_id
        )
        return

    targets = await _get_targets(pool, campaign)
    total = len(targets)
    await pool.execute(
        "UPDATE dm_campaigns SET total_targets=$1 WHERE id=$2", total, campaign_id
    )

    if not targets:
        await pool.execute(
            "UPDATE dm_campaigns SET status='done', finished_at=now() WHERE id=$1",
            campaign_id,
        )
        return

    template = campaign["text_template"]
    acc_cycle = list(accounts)
    acc_idx = 0
    sent = 0
    failed = 0

    for user_id in targets:
        # Проверить не отменена ли кампания
        current = await pool.fetchrow(
            "SELECT status FROM dm_campaigns WHERE id=$1", campaign_id
        )
        if current and current["status"] == "paused":
            log.info("dm_engine: campaign %d paused", campaign_id)
            return

        acc = dict(acc_cycle[acc_idx % len(acc_cycle)])
        acc_idx += 1

        text = expand_spintax(template)
        result = await send_dm(acc["session_str"], user_id, text, _acc=acc)
        status = result["status"]

        if status == "sent":
            sent += 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status) "
                "VALUES ($1,$2,$3,'sent') ON CONFLICT DO NOTHING",
                campaign_id, acc["id"], user_id,
            )
            await pool.execute(
                "UPDATE dm_campaigns SET sent_count=sent_count+1 WHERE id=$1",
                campaign_id,
            )
        elif status == "flood":
            wait = result.get("wait") or _FLOOD_PAUSE
            log.info("dm_engine: flood wait %ds (campaign %d)", wait, campaign_id)
            await asyncio.sleep(min(wait, 300))
            # Сохранить ошибку но не считать как fail — попробуем потом
            continue
        elif status in ("blocked", "auth"):
            # Аккаунт заблокирован/невалиден — перейти на следующий
            log.warning("dm_engine: acc %d status %s, skipping", acc["id"], status)
            acc_cycle = [a for a in acc_cycle if a["id"] != acc["id"]]
            if not acc_cycle:
                log.error("dm_engine: no more accounts for campaign %d", campaign_id)
                break
        else:
            # skip или retry — логируем как ошибку
            failed += 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, error_msg) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                campaign_id, acc["id"], user_id, status, result.get("error", "")[:200],
            )
            await pool.execute(
                "UPDATE dm_campaigns SET fail_count=fail_count+1 WHERE id=$1",
                campaign_id,
            )

        # Humanized delay
        delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
        await asyncio.sleep(delay)

    status_final = "done" if sent + failed >= total else "done"
    await pool.execute(
        "UPDATE dm_campaigns SET status=$1, finished_at=now() WHERE id=$2",
        status_final, campaign_id,
    )

    try:
        await bot.send_message(
            owner_id,
            f"📨 <b>DM-кампания «{campaign['name']}» завершена</b>\n\n"
            f"✅ Отправлено: <b>{sent}</b>\n"
            f"❌ Ошибок: <b>{failed}</b>\n"
            f"📊 Всего целей: <b>{total}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

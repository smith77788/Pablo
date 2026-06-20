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
import logging
import random
import re
import time

import asyncpg
from aiogram import Bot

from services.logger import log_exc_swallow
from services import infra_memory

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
    "UserDeactivatedBan",
    "UserDeactivated",
    "UserNotMutualContact",
    "UserPrivacyRestricted",
    "InputUserDeactivated",
}
_FLOOD_ERRORS = {"FloodWaitError", "FloodWait"}
_BLOCKED_ERRORS = {
    "UserBlockedBan",
    "YouBlockedUser",
    "PeerFloodError",
    "ChatWriteForbidden",
    "UserBannedInChannel",
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
    username: str | None = None,
) -> dict:
    """
    Отправить одно личное сообщение через личный аккаунт Telegram.

    Стратегия адресации (Telethon требует entity с access_hash):
    1. Если есть username — используем его (@handle), это самый надёжный способ.
    2. Если username нет — используем числовой user_id. Telethon попробует
       разрешить его через contacts.GetContacts или по кэшу сессии.
       Если аккаунт ранее видел этого пользователя — работает. Иначе — retry error.

    Возвращает {'status': 'sent'|'flood'|'blocked'|'skip'|'auth'|'retry', 'wait': int, 'error': str}
    """
    from services import account_manager

    # Prefer username for reliable entity resolution in Telethon.
    # Raw integer user_id requires the session to have seen this user before
    # (have their access_hash in cache). Username resolves via contacts.Search.
    target: str | int = username.lstrip("@") if username else user_id
    try:
        await account_manager.send_message(session_str, target, text, _acc=_acc)
        return {"status": "sent"}
    except Exception as exc:
        kind = _classify_error(exc)
        wait = _extract_flood_seconds(exc) if kind == "flood" else 0
        # If username resolution failed and we have user_id, fall back to int
        if kind == "retry" and username and user_id:
            try:
                await account_manager.send_message(session_str, user_id, text, _acc=_acc)
                return {"status": "sent"}
            except Exception as exc2:
                kind2 = _classify_error(exc2)
                wait2 = _extract_flood_seconds(exc2) if kind2 == "flood" else 0
                return {"status": kind2, "wait": wait2, "error": str(exc2)[:200]}
        return {"status": kind, "wait": wait, "error": str(exc)[:200]}


# ── Campaign runner ────────────────────────────────────────────────────────────

_MIN_DELAY = (
    35.0  # минимум секунд между отправками (< 30s = гарантированный spam block)
)
_MAX_DELAY = 90.0  # максимум секунд между отправками
_FLOOD_PAUSE = 120  # пауза при flood (если нет явного wait)


async def _get_targets(pool: asyncpg.Pool, campaign: dict) -> list[dict]:
    """Получить список получателей для кампании.

    Возвращает list[dict] с ключами: user_id (int), username (str | None).
    username используется для надёжной адресации через Telethon (get_entity).
    """
    campaign_id = campaign["id"]
    target_type = campaign["target_type"]
    target_id = campaign["target_id"]

    # Исключить уже успешно отправленных, заблокированных и пропущенных (не retry)
    sent_ids = {
        r["tg_user_id"]
        for r in await pool.fetch(
            "SELECT tg_user_id FROM dm_campaign_log WHERE campaign_id=$1 AND status IN ('sent','blocked','skip')",
            campaign_id,
        )
    }

    if target_type == "bot_users" and target_id:
        # Fetch user_id + username so Telethon can resolve the entity reliably
        rows = await pool.fetch(
            "SELECT DISTINCT ON (user_id) user_id, username "
            "FROM bot_users WHERE bot_id=$1 AND user_id > 0",
            target_id,
        )
        return [
            {"user_id": r["user_id"], "username": r["username"] or None}
            for r in rows
            if r["user_id"] not in sent_ids
        ]
    elif target_type == "crm":
        rows = await pool.fetch(
            "SELECT DISTINCT ON (tg_user_id) tg_user_id, username "
            "FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0",
            campaign["owner_id"],
        )
        return [
            {"user_id": r["tg_user_id"], "username": r["username"] or None}
            for r in rows
            if r["tg_user_id"] not in sent_ids
        ]
    elif target_type == "cohort" and target_id:
        # target_id = bot_id, params.cohort_type = hot|warm|cold|lost
        import json as _json

        params = campaign.get("params") or {}
        if isinstance(params, str):
            try:
                params = _json.loads(params)
            except Exception:
                params = {}
        cohort = params.get("cohort_type", "warm")
        cohort_sql = {
            "hot": "ua.last_seen >= now() - INTERVAL '1 day'",
            "warm": "ua.last_seen >= now() - INTERVAL '7 days' AND ua.last_seen < now() - INTERVAL '1 day'",
            "cold": "ua.last_seen >= now() - INTERVAL '30 days' AND ua.last_seen < now() - INTERVAL '7 days'",
            "lost": "ua.last_seen < now() - INTERVAL '30 days'",
        }.get(cohort, "ua.last_seen >= now() - INTERVAL '7 days'")
        # JOIN bot_users to get username for reliable entity resolution
        rows = await pool.fetch(
            f"SELECT ua.user_id, bu.username "
            f"FROM user_activity ua "
            f"LEFT JOIN bot_users bu ON bu.bot_id = ua.bot_id AND bu.user_id = ua.user_id "
            f"WHERE ua.bot_id=$1 AND {cohort_sql}",
            target_id,
        )
        return [
            {"user_id": r["user_id"], "username": r["username"] or None}
            for r in rows
            if r["user_id"] not in sent_ids
        ]
    elif target_type == "parsed_audience":
        # target_id = parse_run_id (0 = all runs for this owner)
        conditions = "owner_id=$1 AND tg_user_id > 0"
        params_list: list = [campaign["owner_id"]]
        if target_id:
            conditions += " AND parse_run_id=$2"
            params_list.append(target_id)
        rows = await pool.fetch(
            f"SELECT DISTINCT ON (tg_user_id) tg_user_id, username "
            f"FROM parsed_audiences WHERE {conditions}",
            *params_list,
        )
        return [
            {"user_id": r["tg_user_id"], "username": r["username"] or None}
            for r in rows
            if r["tg_user_id"] not in sent_ids
        ]
    return []


async def run_campaign(
    pool: asyncpg.Pool,
    bot: Bot,
    campaign_id: int,
    op_id: int | None = None,
) -> None:
    """Запустить или продолжить DM-кампанию. Вызывается из operation_queue."""
    campaign = await pool.fetchrow(
        "SELECT * FROM dm_campaigns WHERE id=$1", campaign_id
    )
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

    try:
        from services.flood_engine import get_active_accounts

        accounts = await get_active_accounts(pool, owner_id)
    except Exception:
        log.exception(
            "dm_engine: get_active_accounts failed for campaign %d", campaign_id
        )
        await pool.execute(
            "UPDATE dm_campaigns SET status='failed' WHERE id=$1", campaign_id
        )
        return

    if not accounts:
        log.error(
            "dm_engine: no active accounts for campaign %d owner=%d",
            campaign_id,
            owner_id,
        )
        await pool.execute(
            "UPDATE dm_campaigns SET status='failed' WHERE id=$1", campaign_id
        )
        return

    try:
        targets = await _get_targets(pool, campaign)
    except Exception:
        log.exception("dm_engine: _get_targets failed for campaign %d", campaign_id)
        await pool.execute(
            "UPDATE dm_campaigns SET status='failed' WHERE id=$1", campaign_id
        )
        return
    total = len(targets)
    await pool.execute(
        "UPDATE dm_campaigns SET total_targets=$1 WHERE id=$2", total, campaign_id
    )
    if op_id and total:
        try:
            await pool.execute(
                "UPDATE operation_queue SET total_items=$1 WHERE id=$2", total, op_id
            )
        except Exception as _e:
            log.warning("dm_engine: failed to set total_items for op=%s: %s", op_id, _e)

    if not targets:
        await pool.execute(
            "UPDATE dm_campaigns SET status='done', finished_at=now() WHERE id=$1",
            campaign_id,
        )
        return

    template = campaign["text_template"]
    # Brand injection for free-tier users (plain text — DMs don't use HTML parse_mode)
    try:
        from services import brand_injection as _bi
        if await _bi.is_user_free_tier(pool, owner_id):
            template = _bi.add_promo(template, html=False, context="dm")
    except Exception:
        pass
    acc_cycle = list(accounts)
    acc_idx = 0
    sent = 0
    failed = 0
    _notified_milestones: set[int] = set()  # 25, 50, 75

    for target in targets:
        # target is a dict: {user_id: int, username: str | None}
        user_id: int = target["user_id"]
        username: str | None = target.get("username")

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
        t0_dm = time.monotonic()
        result = await send_dm(
            acc["session_str"], user_id, text, _acc=acc, username=username
        )
        status = result["status"]

        if status == "sent":
            sent += 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status) "
                "VALUES ($1,$2,$3,'sent') ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
            )
            await pool.execute(
                "UPDATE dm_campaigns SET sent_count=sent_count+1 WHERE id=$1",
                campaign_id,
            )
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", True, duration_s=time.monotonic() - t0_dm
            )
        elif status == "flood":
            wait = result.get("wait") or _FLOOD_PAUSE
            log.info(
                "dm_engine: flood wait %ds acc=%d (campaign %d)",
                wait,
                acc["id"],
                campaign_id,
            )
            # Установить cooldown_until для аккаунта
            try:
                await pool.execute(
                    "UPDATE tg_accounts SET cooldown_until = NOW() + ($1 * INTERVAL '1 second'), "
                    "last_flood_at = NOW(), flood_count_7d = COALESCE(flood_count_7d, 0) + 1 "
                    "WHERE id=$2",
                    min(wait, 3600),
                    acc["id"],
                )
            except Exception:
                log_exc_swallow(
                    log, "dm_engine: failed to set cooldown_until for acc=%d", acc["id"]
                )
            # Убрать аккаунт из цикла временно и подождать
            if wait <= 60:
                await asyncio.sleep(min(wait, 60))
            else:
                # Для долгих флуд-вейтов — убираем аккаунт из ротации, продолжаем другими
                acc_cycle_without = [a for a in acc_cycle if a["id"] != acc["id"]]
                if acc_cycle_without:
                    acc_cycle = acc_cycle_without
                    log.info(
                        "dm_engine: removed flooded acc %d from rotation, %d remaining",
                        acc["id"],
                        len(acc_cycle),
                    )
                else:
                    await asyncio.sleep(min(wait, 300))
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", False, "flood_wait"
            )
            continue
        elif status in ("blocked", "auth"):
            # Аккаунт заблокирован/невалиден — считать текущую попытку ошибкой
            log.warning("dm_engine: acc %d status %s, skipping", acc["id"], status)
            failed += 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, error_msg) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
                status,
                result.get("error", "")[:200],
            )
            await pool.execute(
                "UPDATE dm_campaigns SET fail_count=fail_count+1 WHERE id=$1",
                campaign_id,
            )
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", False, result.get("error", "")
            )
            acc_cycle = [a for a in acc_cycle if a["id"] != acc["id"]]
            if not acc_cycle:
                log.error(
                    "dm_engine: no more accounts for campaign %d, stopping", campaign_id
                )
                break  # Оставшиеся цели будут учтены после цикла
        elif status == "skip":
            # Пользователь заблокировал бота или деактивирован — не ошибка, пропускаем тихо
            log.debug(
                "dm_engine: user %d blocked/deactivated, skipping silently", user_id
            )
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, error_msg) "
                "VALUES ($1,$2,$3,'skip',$4) ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
                result.get("error", "")[:200],
            )
            # Не считаем как fail — пользователь просто недоступен.
            # Применяем задержку, чтобы не спамить API при большом числе неактивных.
            if op_id:
                try:
                    await pool.execute(
                        "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                        sent + failed,
                        op_id,
                    )
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(_MIN_DELAY, _MAX_DELAY))
            continue
        else:
            # retry — логируем как ошибку
            failed += 1
            await pool.execute(
                "INSERT INTO dm_campaign_log(campaign_id, account_id, tg_user_id, status, error_msg) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING",
                campaign_id,
                acc["id"],
                user_id,
                status,
                result.get("error", "")[:200],
            )
            await pool.execute(
                "UPDATE dm_campaigns SET fail_count=fail_count+1 WHERE id=$1",
                campaign_id,
            )
            infra_memory.record_account_op(
                acc["id"], "dm_campaign", False, result.get("error", "")
            )

        # Sync done_items into operation_queue for progress bar in ops list
        if op_id:
            try:
                await pool.execute(
                    "UPDATE operation_queue SET done_items=$1 WHERE id=$2",
                    sent + failed,
                    op_id,
                )
            except Exception:
                pass

        # Milestone progress notifications (25%, 50%, 75%)
        if total > 0:
            _done = sent + failed
            _pct = int(_done * 100 / total)
            for _milestone in (25, 50, 75):
                if _pct >= _milestone and _milestone not in _notified_milestones:
                    _notified_milestones.add(_milestone)
                    try:
                        from database import db as _db

                        await _db.notify_if_enabled(
                            pool,
                            bot,
                            owner_id,
                            "op_complete",
                            f"📨 <b>DM «{campaign['name']}»</b> — {_milestone}%\n"
                            f"✅ {sent} отправлено · ❌ {failed} ошибок · 📊 {total} всего",
                        )
                    except Exception:
                        log_exc_swallow(
                            log,
                            f"dm_engine: progress notification failed campaign={campaign.get('id')} owner={owner_id}",
                        )

        # Humanized delay
        delay = random.uniform(_MIN_DELAY, _MAX_DELAY)
        await asyncio.sleep(delay)

    # Учесть необработанные цели (напр., при исчерпании всех аккаунтов)
    unprocessed = max(0, total - sent - failed)
    if unprocessed > 0:
        failed += unprocessed
        await pool.execute(
            "UPDATE dm_campaigns SET fail_count=fail_count+$1 WHERE id=$2",
            unprocessed,
            campaign_id,
        )
        log.warning(
            "dm_engine: campaign %d — %d targets unprocessed (accounts exhausted)",
            campaign_id,
            unprocessed,
        )

    status_final = "partial" if unprocessed > 0 else "done"
    await pool.execute(
        "UPDATE dm_campaigns SET status=$1, finished_at=now() WHERE id=$2",
        status_final,
        campaign_id,
    )

    try:
        from database import db as _db

        await _db.notify_if_enabled(
            pool,
            bot,
            owner_id,
            "op_complete",
            f"📨 <b>DM-кампания «{campaign['name']}» завершена</b>\n\n"
            f"✅ Отправлено: <b>{sent}</b>\n"
            f"❌ Ошибок: <b>{failed}</b>\n"
            f"📊 Всего целей: <b>{total}</b>",
        )
    except Exception:
        log_exc_swallow(
            log, "Сбой уведомления о завершении DM-кампании", campaign_id=campaign_id
        )

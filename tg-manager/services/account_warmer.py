"""
Account Warming System — постепенный разогрев новых аккаунтов.

Имитирует натуральное поведение:
- День 1-3: чтение сообщений, просмотр профилей
- День 4-7: лайки/реакции, вступление в каналы
- День 8-14: комментарии, групповые сообщения
- День 15+: полная активность

Все действия логируются в account_warmup_log.
Статус плана хранится в account_warmup_plans.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Optional

import asyncpg
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)

# Публичные каналы/группы для "прогрева" (вступление, чтение)
_WARMUP_PUBLIC_CHANNELS = [
    "@telegram",
    "@durov",
    "@tginfo",
]

# Действия по дням разогрева
_WARMUP_SCHEDULE: dict[str, list[str]] = {
    "days_1_3": ["read_channel", "view_profile", "open_chat"],
    "days_4_7": ["read_channel", "view_profile", "join_channel", "send_reaction"],
    "days_8_14": ["read_channel", "join_channel", "send_reaction", "search"],
    "days_15_plus": [
        "read_channel",
        "join_channel",
        "send_reaction",
        "search",
        "dm_bot",
    ],
}


@dataclass
class WarmupPlan:
    plan_id: int
    account_id: int
    owner_id: int
    current_day: int
    target_days: int
    daily_actions: int
    status: str


async def create_warmup_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    account_id: int,
    plan_type: str = "standard",  # standard / gentle / aggressive
) -> int:
    """Создаёт план разогрева для аккаунта. Возвращает plan_id."""
    daily_map = {"gentle": 3, "standard": 5, "aggressive": 10}
    days_map = {"gentle": 21, "standard": 14, "aggressive": 7}

    row = await pool.fetchrow(
        """INSERT INTO account_warmup_plans(
               owner_id, account_id, plan_type, daily_actions, target_days
           ) VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (account_id) DO UPDATE
               SET status='active', current_day=0, started_at=NOW(),
                   plan_type=$3, daily_actions=$4, target_days=$5
           RETURNING id""",
        owner_id,
        account_id,
        plan_type,
        daily_map.get(plan_type, 5),
        days_map.get(plan_type, 14),
    )
    log.info("warmup: created plan %d for acc=%d", row["id"], account_id)
    return row["id"]


async def get_active_plans(pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    rows = await pool.fetch(
        """SELECT wp.*, a.phone, a.first_name
           FROM account_warmup_plans wp
           JOIN tg_accounts a ON a.id = wp.account_id
           WHERE wp.owner_id=$1 AND wp.status='active'
           ORDER BY wp.started_at""",
        owner_id,
    )
    return [dict(r) for r in rows]


def _get_actions_for_day(day: int) -> list[str]:
    if day <= 3:
        return _WARMUP_SCHEDULE["days_1_3"]
    if day <= 7:
        return _WARMUP_SCHEDULE["days_4_7"]
    if day <= 14:
        return _WARMUP_SCHEDULE["days_8_14"]
    return _WARMUP_SCHEDULE["days_15_plus"]


async def _perform_read_channel(client, channel_ref: str) -> bool:
    """Имитирует чтение канала: открываем, делаем паузу."""
    try:
        entity = await client.get_entity(channel_ref)
        msgs = await client.get_messages(entity, limit=5)
        await asyncio.sleep(random.uniform(3, 8))
        return bool(msgs)
    except Exception as e:
        log_exc_swallow(log, "warmup read_channel %s", channel_ref)
        return False


async def _perform_join_channel(client, channel_ref: str) -> bool:
    """Вступаем в публичный канал."""
    try:
        from telethon.tl.functions.channels import JoinChannelRequest

        entity = await client.get_entity(channel_ref)
        await client(JoinChannelRequest(entity))
        await asyncio.sleep(random.uniform(2, 5))
        return True
    except Exception as e:
        log_exc_swallow(log, "warmup join_channel %s", channel_ref)
        return False


async def _perform_search(client, query: str) -> bool:
    """Имитирует поиск в Telegram."""
    try:
        from telethon.tl.functions.contacts import SearchRequest

        await client(SearchRequest(q=query, limit=5))
        await asyncio.sleep(random.uniform(2, 6))
        return True
    except Exception as e:
        log_exc_swallow(log, "warmup search %s", query)
        return False


async def _log_warmup_action(
    pool: asyncpg.Pool,
    account_id: int,
    action_type: str,
    target: str,
    success: bool,
    error: str | None = None,
) -> None:
    try:
        await pool.execute(
            """INSERT INTO account_warmup_log(account_id, action_type, target, success, error)
               VALUES ($1,$2,$3,$4,$5)""",
            account_id,
            action_type,
            target,
            success,
            error,
        )
    except Exception as e:
        log.debug("warmup log write: %s", e)


async def run_daily_warmup(
    pool: asyncpg.Pool,
    plan: dict,
) -> dict:
    """
    Выполняет дневные действия для одного плана разогрева.
    Возвращает {'actions_done', 'actions_ok', 'actions_fail', 'completed'}.
    """
    from services import account_manager

    account_id = plan["account_id"]
    owner_id = plan["owner_id"]
    current_day = plan["current_day"]
    daily_actions = plan["daily_actions"]
    plan_id = plan["id"]

    # Получаем сессию аккаунта
    acc_row = await pool.fetchrow(
        """SELECT a.session_str, a.device_model, a.system_version, a.app_version, p.proxy_url
           FROM tg_accounts a
           LEFT JOIN user_proxies p ON p.id=a.proxy_id AND p.is_active=TRUE
           WHERE a.id=$1 AND a.is_active=TRUE""",
        account_id,
    )
    if not acc_row:
        log.warning("warmup: account %d not found or inactive", account_id)
        return {
            "actions_done": 0,
            "actions_ok": 0,
            "actions_fail": 0,
            "completed": False,
        }

    device = dict(acc_row) if acc_row["device_model"] else None
    client = account_manager._make_client(acc_row["session_str"], device)

    actions_ok = 0
    actions_fail = 0
    available_actions = _get_actions_for_day(current_day)
    channels = _WARMUP_PUBLIC_CHANNELS.copy()
    random.shuffle(channels)

    try:
        await asyncio.wait_for(client.connect(), timeout=15)

        for i in range(daily_actions):
            action = random.choice(available_actions)
            target = channels[i % len(channels)]
            success = False
            error = None

            try:
                if action == "read_channel":
                    success = await _perform_read_channel(client, target)
                elif action == "join_channel":
                    success = await _perform_join_channel(client, target)
                elif action == "search":
                    queries = ["telegram", "news", "crypto", "tech", "sport"]
                    success = await _perform_search(client, random.choice(queries))
                    target = "search"
                elif action in ("view_profile", "open_chat", "send_reaction", "dm_bot"):
                    # Упрощённая имитация: просто пауза
                    await asyncio.sleep(random.uniform(2, 7))
                    success = True
            except Exception as e:
                error = str(e)[:100]
                success = False

            await _log_warmup_action(pool, account_id, action, target, success, error)

            if success:
                actions_ok += 1
            else:
                actions_fail += 1

            # Пауза между действиями (имитация человека)
            await asyncio.sleep(random.uniform(30, 120))

    except Exception as e:
        log.warning("warmup session error acc=%d: %s", account_id, e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "сбой disconnect при разогреве аккаунта")

    # Обновляем план — только если было хотя бы частичное выполнение
    # Если все действия провалились, повторяем тот же день на следующем цикле
    if actions_ok > 0:
        new_day = current_day + 1
    else:
        log.warning("warmup: all %d actions failed for acc=%d, retrying same day %d",
                    daily_actions, account_id, current_day)
        new_day = current_day

    completed = new_day >= plan["target_days"]
    new_status = "completed" if completed else "active"

    await pool.execute(
        """UPDATE account_warmup_plans
           SET current_day=$1, status=$2, last_action_at=NOW(),
               completed_at=CASE WHEN $2='completed' THEN NOW() ELSE NULL END
           WHERE id=$3""",
        new_day,
        new_status,
        plan_id,
    )

    if completed and actions_ok > 0:
        # После успешного завершения разогрева повышаем trust_score
        await pool.execute(
            "UPDATE tg_accounts SET trust_score = LEAST(trust_score + 0.3, 1.0) WHERE id=$1",
            account_id,
        )

    return {
        "actions_done": actions_ok + actions_fail,
        "actions_ok": actions_ok,
        "actions_fail": actions_fail,
        "completed": completed,
    }


async def run_warmup_loop(pool: asyncpg.Pool, interval_hours: int = 6) -> None:
    """
    Фоновый цикл: каждые N часов выполняет действия разогрева для всех активных планов.
    Один запуск в день на план (проверяем last_action_at).
    """
    import asyncio

    while True:
        try:
            # Найти планы, которые не запускались сегодня
            rows = await pool.fetch(
                """SELECT wp.*, a.owner_id
                   FROM account_warmup_plans wp
                   JOIN tg_accounts a ON a.id = wp.account_id
                   WHERE wp.status = 'active'
                     AND (wp.last_action_at IS NULL
                          OR wp.last_action_at < NOW() - INTERVAL '20 hours')""",
            )
            for plan in rows:
                await run_daily_warmup(pool, dict(plan))
                await asyncio.sleep(30)  # Пауза между аккаунтами
        except Exception as e:
            log.warning("warmup loop error: %s", e)
        await asyncio.sleep(interval_hours * 3600)

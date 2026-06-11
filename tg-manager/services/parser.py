"""
Audience Parser Framework — извлечение аудитории из каналов/групп.

Поддерживает:
- participants: все участники канала/группы
- active: пользователи с недавними сообщениями
- commenters: авторы комментариев к постам
- reaction_givers: пользователи оставившие реакции

Результаты сохраняются в parsed_audiences (дедупликация по owner+source+user_id).
История запусков — в parser_runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from services.logger import log_exc_swallow
from services import infra_memory

import asyncpg

log = logging.getLogger(__name__)


async def _get_best_account(pool: asyncpg.Pool, owner_id: int) -> dict | None:
    from services.flood_engine import get_best_account

    return await get_best_account(pool, owner_id, action_type="parse")


async def _create_run(
    pool: asyncpg.Pool,
    owner_id: int,
    source_type: str,
    source_ref: str,
    parse_type: str,
    account_id: int | None,
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO parser_runs(owner_id, source_type, source_ref, parse_type, account_id)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        owner_id,
        source_type,
        source_ref,
        parse_type,
        account_id,
    )
    return row["id"]


async def _update_run(
    pool: asyncpg.Pool,
    run_id: int,
    status: str,
    total_found: int = 0,
    total_saved: int = 0,
    error: str | None = None,
) -> None:
    await pool.execute(
        """UPDATE parser_runs
           SET status=$1, total_found=$2, total_saved=$3, error=$4, finished_at=NOW()
           WHERE id=$5""",
        status,
        total_found,
        total_saved,
        error,
        run_id,
    )


async def _save_users(
    pool: asyncpg.Pool,
    owner_id: int,
    run_id: int,
    source_type: str,
    source_id: int,
    source_title: str,
    source_username: str,
    users: list[dict],
) -> int:
    saved = 0
    for u in users:
        try:
            result = await pool.execute(
                """INSERT INTO parsed_audiences(
                       owner_id, source_type, source_id, source_title, source_username,
                       parse_run_id, tg_user_id, username, first_name, last_name,
                       is_premium, is_bot, parsed_at
                   ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,NOW())
                   ON CONFLICT (owner_id, source_id, tg_user_id) DO UPDATE
                   SET parse_run_id=$6, username=$8, first_name=$9, last_name=$10,
                       is_premium=$11, parsed_at=NOW()""",
                owner_id,
                source_type,
                source_id,
                source_title,
                source_username,
                run_id,
                u["id"],
                u.get("username"),
                u.get("first_name"),
                u.get("last_name"),
                bool(u.get("premium")),
                bool(u.get("bot")),
            )
            if "INSERT" in str(result):
                saved += 1
        except Exception as e:
            log.debug("parser save user %s: %s", u.get("id"), e)
    return saved


async def parse_members(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    limit: int = 5000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
) -> dict:
    """
    Парсинг участников канала/группы.
    progress_cb(current, total) вызывается каждые 200 пользователей.
    Возвращает {'run_id', 'total_found', 'total_saved', 'status'}.
    """
    from services import account_manager
    from telethon.tl.functions.channels import GetParticipantsRequest
    from telethon.tl.types import ChannelParticipantsSearch

    acc = await _get_best_account(pool, owner_id)
    if not acc:
        return {"status": "error", "error": "Нет доступных аккаунтов"}

    run_id = await _create_run(
        pool, owner_id, "channel", source_ref, "members", acc["id"]
    )

    client = account_manager._make_client(acc["session_str"], acc)
    t0_parse = time.monotonic()
    total_found = 0
    total_saved = 0
    source_id = 0
    source_title = source_ref
    source_username = source_ref.lstrip("@")

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        try:
            entity = await client.get_entity(source_ref)
            source_id = entity.id
            source_title = getattr(entity, "title", source_ref)
            source_username = getattr(entity, "username", "") or source_ref.lstrip("@")
        except Exception as e:
            await _update_run(
                pool, run_id, "failed", error=f"Не удалось получить сущность: {e}"
            )
            infra_memory.record_account_op(
                acc["id"],
                "parse",
                False,
                str(e)[:100],
                duration_s=time.monotonic() - t0_parse,
            )
            return {"status": "error", "error": str(e), "run_id": run_id}

        offset = 0
        batch_size = 200
        while total_found < limit:
            try:
                result = await client(
                    GetParticipantsRequest(
                        entity,
                        ChannelParticipantsSearch(""),
                        offset=offset,
                        limit=min(batch_size, limit - total_found),
                        hash=0,
                    )
                )
                if not result.users:
                    break

                users = [
                    {
                        "id": u.id,
                        "username": u.username,
                        "first_name": u.first_name,
                        "last_name": u.last_name,
                        "premium": getattr(u, "premium", False),
                        "bot": u.bot,
                    }
                    for u in result.users
                    if not u.deleted
                ]

                batch_saved = await _save_users(
                    pool,
                    owner_id,
                    run_id,
                    "channel",
                    source_id,
                    source_title,
                    source_username,
                    users,
                )
                total_found += len(users)
                total_saved += batch_saved
                offset += len(result.users)

                if progress_cb:
                    try:
                        await progress_cb(
                            total_found, min(limit, getattr(result, "count", limit))
                        )
                    except Exception:
                        log_exc_swallow(log, "Сбой progress_cb в parser")

                if len(result.users) < batch_size:
                    break

                await asyncio.sleep(1.5)  # Anti-flood pause

            except Exception as e:
                err = str(e)
                if "FloodWait" in err:
                    wait = 30
                    try:
                        wait = int(err.split("wait ")[1].split(" ")[0])
                    except Exception:
                        log_exc_swallow(log, "Не удалось распарсить FloodWait в parser")
                        wait = 30
                    log.info("parser FloodWait %ds, sleeping", wait)
                    await asyncio.sleep(min(wait + 5, 120))
                    continue
                log.warning("parser GetParticipants error: %s", e)
                break

    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в parser")

    status = "done" if total_found > 0 else "empty"
    await _update_run(pool, run_id, status, total_found, total_saved)
    _parse_dur = time.monotonic() - t0_parse
    if total_found > 0:
        infra_memory.record_account_op(acc["id"], "parse", True, duration_s=_parse_dur)
    else:
        infra_memory.record_account_op(
            acc["id"], "parse", False, "no_users_found", duration_s=_parse_dur
        )
    return {
        "run_id": run_id,
        "status": status,
        "total_found": total_found,
        "total_saved": total_saved,
        "source_title": source_title,
    }


async def parse_active_users(
    pool: asyncpg.Pool,
    owner_id: int,
    source_ref: str,
    days_back: int = 30,
    limit: int = 2000,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
) -> dict:
    """
    Парсинг активных пользователей: те, кто писал в группе за последние N дней.
    Работает только для супергрупп (не каналов).
    """
    from services import account_manager

    acc = await _get_best_account(pool, owner_id)
    if not acc:
        return {"status": "error", "error": "Нет доступных аккаунтов"}

    run_id = await _create_run(pool, owner_id, "group", source_ref, "active", acc["id"])

    client = account_manager._make_client(acc["session_str"], acc)
    t0_parse = time.monotonic()
    total_found = 0
    total_saved = 0
    source_id = 0
    source_title = source_ref

    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        try:
            entity = await client.get_entity(source_ref)
            source_id = entity.id
            source_title = getattr(entity, "title", source_ref)
        except Exception as e:
            await _update_run(pool, run_id, "failed", error=str(e))
            infra_memory.record_account_op(
                acc["id"],
                "parse",
                False,
                str(e)[:100],
                duration_s=time.monotonic() - t0_parse,
            )
            return {"status": "error", "error": str(e), "run_id": run_id}

        seen_ids: set[int] = set()
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

        async for msg in client.iter_messages(entity, limit=5000):
            if msg.date < cutoff:
                break
            if not msg.sender_id or msg.sender_id in seen_ids:
                continue
            seen_ids.add(msg.sender_id)

            user = None
            try:
                user = await client.get_entity(msg.sender_id)
            except Exception:
                log_exc_swallow(
                    log, "Сбой get_entity в parser", sender_id=msg.sender_id
                )

            if user:
                users_batch = [
                    {
                        "id": user.id,
                        "username": getattr(user, "username", None),
                        "first_name": getattr(user, "first_name", None),
                        "last_name": getattr(user, "last_name", None),
                        "premium": getattr(user, "premium", False),
                        "bot": getattr(user, "bot", False),
                    }
                ]
                saved = await _save_users(
                    pool,
                    owner_id,
                    run_id,
                    "group",
                    source_id,
                    source_title,
                    source_ref.lstrip("@"),
                    users_batch,
                )
                total_found += 1
                total_saved += saved

            if total_found >= limit:
                break

            # Progress: first update at 1 user found, then every 50 after that.
            # Without this, parses returning <50 users show no progress at all.
            if total_found == 1 or total_found % 50 == 0:
                if progress_cb:
                    try:
                        await progress_cb(total_found, limit)
                    except Exception:
                        log_exc_swallow(log, "Сбой progress_cb в parser (reactors)")
                await asyncio.sleep(0.5)

    finally:
        try:
            await client.disconnect()
        except Exception:
            log_exc_swallow(log, "Сбой disconnect клиента в parser")

    status = "done" if total_found > 0 else "empty"
    await _update_run(pool, run_id, status, total_found, total_saved)
    _parse_dur = time.monotonic() - t0_parse
    if total_found > 0:
        infra_memory.record_account_op(acc["id"], "parse", True, duration_s=_parse_dur)
    else:
        infra_memory.record_account_op(
            acc["id"], "parse", False, "no_users_found", duration_s=_parse_dur
        )
    return {
        "run_id": run_id,
        "status": status,
        "total_found": total_found,
        "total_saved": total_saved,
        "source_title": source_title,
    }


async def get_parsed_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    source_id: int | None = None,
    run_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    active_only: bool = False,
) -> list[dict]:
    """Получить сохранённую аудиторию с фильтрами."""
    conditions = ["owner_id=$1"]
    params: list = [owner_id]
    p = 2

    if source_id:
        conditions.append(f"source_id=${p}")
        params.append(source_id)
        p += 1

    if run_id:
        conditions.append(f"parse_run_id=${p}")
        params.append(run_id)
        p += 1

    if active_only:
        conditions.append("is_active=TRUE")

    where = " AND ".join(conditions)
    rows = await pool.fetch(
        f"SELECT * FROM parsed_audiences WHERE {where} "
        f"ORDER BY parsed_at DESC OFFSET ${p} LIMIT ${p + 1}",
        *params,
        offset,
        limit,
    )
    return [dict(r) for r in rows]


async def get_run_history(
    pool: asyncpg.Pool, owner_id: int, limit: int = 20
) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, source_type, source_ref, parse_type, status,
                  total_found, total_saved, started_at, finished_at
           FROM parser_runs WHERE owner_id=$1
           ORDER BY started_at DESC LIMIT $2""",
        owner_id,
        limit,
    )
    return [dict(r) for r in rows]


async def delete_audience(
    pool: asyncpg.Pool,
    owner_id: int,
    source_id: int | None = None,
    run_id: int | None = None,
) -> int:
    """Удалить спарсенную аудиторию. Возвращает число удалённых строк."""
    if run_id:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2",
            owner_id,
            run_id,
        )
    elif source_id:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1 AND source_id=$2",
            owner_id,
            source_id,
        )
    else:
        result = await pool.execute(
            "DELETE FROM parsed_audiences WHERE owner_id=$1", owner_id
        )
    try:
        return int(str(result).split()[-1])
    except Exception:
        log_exc_swallow(
            log, "Не удалось распарсить количество удалённых строк аудитории"
        )
        return 0

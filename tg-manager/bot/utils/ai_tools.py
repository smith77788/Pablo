"""Tool implementations for AI assistant — each tool is isolated per user_id.

READ tools return data. ACTION tools return a pending_action dict that requires
confirmation before execution. The executor function runs confirmed actions.
"""

from __future__ import annotations
import asyncpg
import json
import logging

log = logging.getLogger(__name__)

# ── READ TOOLS ────────────────────────────────────────────────────────────────


async def get_my_bots(pool: asyncpg.Pool, user_id: int) -> dict:
    bots = await pool.fetch(
        """
        SELECT b.bot_id, b.username, b.first_name,
               COUNT(DISTINCT a.user_id) AS audience,
               b.swarm_enabled, b.bot_role, b.cluster, b.token
        FROM managed_bots b
        LEFT JOIN bot_users a ON a.bot_id = b.bot_id AND a.is_active = TRUE
        WHERE b.added_by=$1 AND b.is_active=TRUE
        GROUP BY b.bot_id, b.username, b.first_name, b.swarm_enabled, b.bot_role, b.cluster, b.token
        ORDER BY audience DESC
        """,
        user_id,
    )
    return {
        "total": len(bots),
        "bots": [
            {
                "id": b["bot_id"],
                "name": f"@{b['username']}" if b["username"] else b["first_name"],
                "audience": b["audience"],
                "swarm": b["swarm_enabled"],
                "role": b["bot_role"],
                "cluster": b["cluster"] or "default",
            }
            for b in bots
        ],
    }


async def get_bot_details(pool: asyncpg.Pool, user_id: int, bot_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id,
        user_id,
    )
    if not row:
        return {"error": "Бот не найден или не принадлежит вам"}
    audience = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE", bot_id
        )
        or 0
    )
    today = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE "
            "AND first_seen > now() - INTERVAL '24 hours'",
            bot_id,
        )
        or 0
    )
    broadcasts = (
        await pool.fetchval("SELECT COUNT(*) FROM broadcasts WHERE bot_id=$1", bot_id)
        or 0
    )
    return {
        "id": bot_id,
        "name": f"@{row['username']}" if row["username"] else row["first_name"],
        "username": row["username"] or "",
        "description": (row.get("description") or "")[:200],
        "short_description": (row.get("short_description") or "")[:100],
        "audience_total": int(audience),
        "new_today": int(today),
        "broadcasts_total": int(broadcasts),
        "swarm": row["swarm_enabled"],
        "cluster": row["cluster"] or "default",
        "role": row.get("bot_role") or "general",
    }


async def get_network_stats(pool: asyncpg.Pool, user_id: int) -> dict:
    total_bots = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND is_active=TRUE",
            user_id,
        )
        or 0
    )
    total_audience = (
        await pool.fetchval(
            "SELECT COUNT(DISTINCT a.user_id) FROM bot_users a "
            "JOIN managed_bots b ON b.bot_id=a.bot_id WHERE b.added_by=$1 AND a.is_active=TRUE",
            user_id,
        )
        or 0
    )
    total_sent = (
        await pool.fetchval(
            "SELECT COALESCE(SUM(sent_count),0) FROM broadcasts b2 "
            "JOIN managed_bots m ON m.bot_id=b2.bot_id WHERE m.added_by=$1",
            user_id,
        )
        or 0
    )
    swarm_bots = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM managed_bots WHERE added_by=$1 AND swarm_enabled=true",
            user_id,
        )
        or 0
    )
    return {
        "total_bots": int(total_bots),
        "unique_audience": int(total_audience),
        "messages_sent": int(total_sent),
        "swarm_bots": int(swarm_bots),
    }


async def get_audience_activity(pool: asyncpg.Pool, user_id: int, bot_id: int) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id,
        user_id,
    )
    if not row:
        return {"error": "Бот не найден"}
    hot = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
            "AND last_seen > now() - INTERVAL '24 hours'",
            bot_id,
        )
        or 0
    )
    warm = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
            "AND last_seen BETWEEN now() - INTERVAL '7 days' AND now() - INTERVAL '24 hours'",
            bot_id,
        )
        or 0
    )
    cold = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
            "AND last_seen BETWEEN now() - INTERVAL '30 days' AND now() - INTERVAL '7 days'",
            bot_id,
        )
        or 0
    )
    lost = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 "
            "AND last_seen < now() - INTERVAL '30 days'",
            bot_id,
        )
        or 0
    )
    return {"hot": int(hot), "warm": int(warm), "cold": int(cold), "lost": int(lost)}


async def get_growth_trend(
    pool: asyncpg.Pool, user_id: int, bot_id: int, days: int = 7
) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        bot_id,
        user_id,
    )
    if not row:
        return {"error": "Бот не найден"}
    rows = await pool.fetch(
        """SELECT DATE(first_seen) AS day, COUNT(*) AS new_users
           FROM bot_users
           WHERE bot_id=$1 AND first_seen > now() - ($2 || ' days')::INTERVAL
           GROUP BY day ORDER BY day""",
        bot_id,
        str(days),
    )
    return {
        "period_days": days,
        "daily": [{"date": str(r["day"]), "new_users": r["new_users"]} for r in rows],
        "total_new": sum(r["new_users"] for r in rows),
    }


async def get_seo_recommendations(
    pool: asyncpg.Pool, user_id: int, bot_id: int
) -> dict:
    row = await pool.fetchrow(
        "SELECT * FROM managed_bots WHERE bot_id=$1 AND added_by=$2", bot_id, user_id
    )
    if not row:
        return {"error": "Бот не найден"}
    tips = []
    score = 0
    name = row.get("first_name") or ""
    if len(name) >= 5:
        score += 20
    else:
        tips.append("Имя слишком короткое — добавьте ключевые слова (мин. 5 символов)")
    if row.get("username"):
        score += 15
        if len(row["username"]) <= 20:
            score += 5
    else:
        tips.append("Нет username — бот не будет индексироваться в поиске Telegram")
    if row.get("description"):
        score += 30
        if len(row.get("description", "")) >= 100:
            score += 10
    else:
        tips.append("Нет описания — добавьте текст с ключевыми словами (≥100 символов)")
    if row.get("short_description"):
        score += 20
    else:
        tips.append("Нет краткого описания (about) — оно показывается в превью поиска")
    return {"seo_score": min(score, 100), "tips": tips[:5]}


async def get_my_accounts(pool: asyncpg.Pool, user_id: int) -> dict:
    rows = await pool.fetch(
        "SELECT id, first_name, phone, username, is_active FROM tg_accounts WHERE owner_id=$1 ORDER BY id",
        user_id,
    )
    return {
        "total": len(rows),
        "accounts": [
            {
                "id": r["id"],
                "name": r["first_name"] or "",
                "phone": r["phone"] or "",
                "username": r["username"] or "",
                "is_active": r["is_active"],
            }
            for r in rows
        ],
    }


async def get_my_channels(pool: asyncpg.Pool, user_id: int) -> dict:
    rows = await pool.fetch(
        "SELECT channel_id, title, username, acc_id FROM managed_channels WHERE owner_id=$1 ORDER BY title",
        user_id,
    )
    return {
        "total": len(rows),
        "channels": [
            {
                "id": r["channel_id"],
                "title": r["title"] or "",
                "username": r["username"] or "",
                "acc_id": r["acc_id"],
            }
            for r in rows
        ],
    }


async def get_broadcast_history(
    pool: asyncpg.Pool, user_id: int, bot_id: int, limit: int = 10
) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        bot_id,
        user_id,
    )
    if not row:
        return {"error": "Бот не найден"}
    rows = await pool.fetch(
        """SELECT id, status, total_users, sent_count, failed_count, created_at, finished_at,
                  LEFT(message_text, 100) AS preview
           FROM broadcasts WHERE bot_id=$1 ORDER BY created_at DESC LIMIT $2""",
        bot_id,
        limit,
    )
    return {
        "broadcasts": [
            {
                "id": r["id"],
                "status": r["status"],
                "total": r["total_users"],
                "sent": r["sent_count"],
                "failed": r["failed_count"],
                "preview": r["preview"] or "",
                "created": str(r["created_at"])[:16],
                "finished": str(r["finished_at"])[:16] if r["finished_at"] else None,
            }
            for r in rows
        ]
    }


# ── ACTION TOOLS (return pending_action, require confirmation) ─────────────────


async def action_launch_broadcast(
    pool: asyncpg.Pool, user_id: int, bot_id: int, text: str
) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id, token, first_name, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id,
        user_id,
    )
    if not row:
        return {"error": "Бот не найден или не принадлежит вам"}
    audience = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE",
            bot_id,
        )
        or 0
    )
    name = f"@{row['username']}" if row["username"] else row["first_name"]
    return {
        "pending_action": "launch_broadcast",
        "bot_id": bot_id,
        "bot_name": name,
        "text": text,
        "audience": int(audience),
        "preview": f"Рассылка для {name}: {text[:80]}{'...' if len(text) > 80 else ''}\nПолучателей: {audience}",
    }


async def action_update_bot_profile(
    pool: asyncpg.Pool,
    user_id: int,
    bot_id: int,
    name: str | None = None,
    description: str | None = None,
    short_description: str | None = None,
) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id, token, first_name, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id,
        user_id,
    )
    if not row:
        return {"error": "Бот не найден или не принадлежит вам"}
    if not any([name, description, short_description]):
        return {
            "error": "Укажите хотя бы одно поле для обновления: name, description или short_description"
        }
    bot_name = f"@{row['username']}" if row["username"] else row["first_name"]
    changes = []
    if name:
        changes.append(f"имя: «{name}»")
    if description:
        changes.append(
            f"описание: «{description[:50]}{'...' if len(description) > 50 else ''}»"
        )
    if short_description:
        changes.append(
            f"краткое описание: «{short_description[:50]}{'...' if len(short_description) > 50 else ''}»"
        )
    return {
        "pending_action": "update_bot_profile",
        "bot_id": bot_id,
        "bot_name": bot_name,
        "name": name,
        "description": description,
        "short_description": short_description,
        "preview": f"Обновить профиль {bot_name}: {', '.join(changes)}",
    }


async def action_post_to_channel(
    pool: asyncpg.Pool,
    user_id: int,
    channel_id: int,
    text: str,
) -> dict:
    ch_row = await pool.fetchrow(
        "SELECT channel_id, title, username, acc_id, access_hash FROM managed_channels WHERE owner_id=$1 AND channel_id=$2",
        user_id,
        channel_id,
    )
    if not ch_row:
        return {
            "error": "Канал не найден в кэше. Сначала откройте «Мои каналы» чтобы обновить кэш."
        }
    acc_row = await pool.fetchrow(
        "SELECT id, first_name, phone FROM tg_accounts WHERE owner_id=$1 AND id=$2 AND is_active=TRUE",
        user_id,
        ch_row["acc_id"],
    )
    if not acc_row:
        return {"error": "Аккаунт канала не найден или не активен"}
    ch_name = ch_row["title"] or (
        f"@{ch_row['username']}" if ch_row["username"] else f"id={channel_id}"
    )
    return {
        "pending_action": "post_to_channel",
        "channel_id": channel_id,
        "channel_name": ch_name,
        "acc_id": ch_row["acc_id"],
        "access_hash": ch_row["access_hash"] or 0,
        "text": text,
        "preview": f"Опубликовать в {ch_name}: {text[:80]}{'...' if len(text) > 80 else ''}",
    }


# ── EXECUTOR (runs confirmed actions) ─────────────────────────────────────────


async def action_create_channel(
    pool: asyncpg.Pool,
    user_id: int,
    title: str,
    about: str = "",
    username: str = "",
    is_group: bool = False,
) -> dict:
    acc_row = await pool.fetchrow(
        "SELECT id, first_name, phone FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
        user_id,
    )
    if not acc_row:
        return {"error": "Нет активных аккаунтов. Подключите аккаунт через /accounts"}
    kind = "группу" if is_group else "канал"
    uname_str = f", username: @{username}" if username else ""
    return {
        "pending_action": "create_channel",
        "acc_id": acc_row["id"],
        "title": title,
        "about": about,
        "username": username,
        "is_group": is_group,
        "preview": f"Создать {kind} «{title}» через @{acc_row['phone']}{uname_str}\nОписание: {about[:60] if about else '(нет)'}",
    }


async def action_create_bot(
    pool: asyncpg.Pool,
    user_id: int,
    name: str,
    username: str = "",
    description: str = "",
) -> dict:
    """Создать нового бота через BotFather используя аккаунт с наивысшим trust_score."""
    acc_row = await pool.fetchrow(
        "SELECT id, first_name, phone FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
        user_id,
    )
    if not acc_row:
        return {"error": "Нет активных аккаунтов. Подключите аккаунт через /accounts"}
    uname_str = f", @{username}" if username else ""
    desc_str = (
        f"\nОписание: {description[:60]}{'...' if len(description) > 60 else ''}"
        if description
        else ""
    )
    return {
        "pending_action": "create_bot",
        "acc_id": acc_row["id"],
        "name": name,
        "username": username,
        "description": description,
        "preview": f"Создать бота «{name}» через @{acc_row['phone']}{uname_str}{desc_str}",
    }


async def action_create_group(
    pool: asyncpg.Pool,
    user_id: int,
    title: str,
    about: str = "",
    username: str = "",
) -> dict:
    """Создать группу/супергруппу. Отдельный инструмент для ясности AI."""
    acc_row = await pool.fetchrow(
        "SELECT id, first_name, phone FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
        user_id,
    )
    if not acc_row:
        return {"error": "Нет активных аккаунтов. Подключите аккаунт через /accounts"}
    uname_str = f", @{username}" if username else ""
    return {
        "pending_action": "create_group",
        "acc_id": acc_row["id"],
        "title": title,
        "about": about,
        "username": username,
        "preview": f"Создать группу «{title}» через @{acc_row['phone']}{uname_str}\nОписание: {about[:60] if about else '(нет)'}",
    }


async def action_bulk_create_channels(
    pool: asyncpg.Pool,
    user_id: int,
    prefix: str,
    count: int = 5,
    about: str = "",
    username_pattern: str = "",
    acc_id: int = 0,
) -> dict:
    """Подготовить массовое создание каналов. Будет поставлено в operation_queue."""
    if count < 1 or count > 50:
        return {"error": "Количество каналов должно быть от 1 до 50"}
    if not prefix or len(prefix) < 2:
        return {"error": "Префикс названия должен быть минимум 2 символа"}
    if acc_id:
        acc_row = await pool.fetchrow(
            "SELECT id, first_name, phone FROM tg_accounts WHERE owner_id=$1 AND id=$2 AND is_active=TRUE",
            user_id,
            acc_id,
        )
    else:
        acc_row = await pool.fetchrow(
            "SELECT id, first_name, phone FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
            "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
            user_id,
        )
    if not acc_row:
        return {"error": "Нет активных аккаунтов. Подключите аккаунт через /accounts"}
    return {
        "pending_action": "bulk_create_channels",
        "acc_id": acc_row["id"],
        "prefix": prefix,
        "count": count,
        "about": about,
        "username_pattern": username_pattern,
        "preview": f"Массовое создание {count} каналов «{prefix} #1–#{count}» через @{acc_row['phone']}"
        + (f" с шаблоном @{username_pattern}" if username_pattern else ""),
    }


async def action_schedule_broadcast(
    pool: asyncpg.Pool,
    user_id: int,
    bot_id: int,
    text: str,
    when_minutes: int = 60,
) -> dict:
    row = await pool.fetchrow(
        "SELECT bot_id, first_name, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
        bot_id,
        user_id,
    )
    if not row:
        return {"error": "Бот не найден или не принадлежит вам"}
    audience = (
        await pool.fetchval(
            "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE",
            bot_id,
        )
        or 0
    )
    name = f"@{row['username']}" if row["username"] else row["first_name"]
    return {
        "pending_action": "schedule_broadcast",
        "bot_id": bot_id,
        "bot_name": name,
        "text": text,
        "when_minutes": when_minutes,
        "audience": int(audience),
        "preview": f"Запланировать рассылку для {name} через {when_minutes} мин.\nПолучателей: {audience}\nТекст: {text[:80]}...",
    }


async def execute_action(
    action_data: dict, pool: asyncpg.Pool, user_id: int, http=None
) -> str:
    """Execute a confirmed pending action. Returns result string."""
    name = action_data.get("pending_action")

    if name == "launch_broadcast":
        bot_id = action_data["bot_id"]
        text = action_data["text"]
        row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id,
            user_id,
        )
        if not row or not http:
            return "❌ Ошибка: бот не найден или HTTP сессия недоступна"
        from database import db
        from services import broadcaster

        user_ids_rows = await pool.fetch(
            "SELECT user_id FROM bot_users WHERE bot_id=$1 AND is_active=TRUE AND is_blocked=FALSE",
            bot_id,
        )
        ids = [r["user_id"] for r in user_ids_rows]
        if not ids:
            return "⚠️ Аудитория пуста — нет активных получателей"
        bc_id = await db.create_broadcast(pool, bot_id, text, len(ids), user_id)
        broadcaster.start(pool, http, bc_id, row["token"], bot_id, text, None, ids)
        return f"✅ Рассылка #{bc_id} запущена! Получателей: {len(ids)}"

    elif name == "update_bot_profile":
        bot_id = action_data["bot_id"]
        new_name = action_data.get("name")
        description = action_data.get("description")
        short_desc = action_data.get("short_description")
        row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id,
            user_id,
        )
        if not row or not http:
            return "❌ Ошибка: бот не найден"
        from services import bot_api

        results = []
        if new_name:
            ok = await bot_api.set_name(http, row["token"], new_name)
            if ok:
                await pool.execute(
                    "UPDATE managed_bots SET first_name=$1 WHERE bot_id=$2",
                    new_name,
                    bot_id,
                )
                results.append(f"✅ Имя обновлено: «{new_name}»")
            else:
                results.append("❌ Не удалось обновить имя")
        if description:
            ok = await bot_api.set_description(http, row["token"], description)
            if ok:
                await pool.execute(
                    "UPDATE managed_bots SET description=$1 WHERE bot_id=$2",
                    description,
                    bot_id,
                )
                results.append(f"✅ Описание обновлено ({len(description)} симв.)")
            else:
                results.append("❌ Не удалось обновить описание")
        if short_desc:
            ok = await bot_api.set_short_description(http, row["token"], short_desc)
            if ok:
                await pool.execute(
                    "UPDATE managed_bots SET short_description=$1 WHERE bot_id=$2",
                    short_desc,
                    bot_id,
                )
                results.append("✅ Краткое описание обновлено")
            else:
                results.append("❌ Не удалось обновить краткое описание")
        return "\n".join(results) if results else "❌ Нет изменений"

    elif name == "post_to_channel":
        channel_id = action_data["channel_id"]
        acc_id = action_data["acc_id"]
        access_hash = action_data.get("access_hash", 0) or 0
        text = action_data["text"]
        acc_row = await pool.fetchrow(
            "SELECT session_str FROM tg_accounts WHERE owner_id=$1 AND id=$2 AND is_active=TRUE",
            user_id,
            acc_id,
        )
        if not acc_row:
            return "❌ Аккаунт не найден или не активен"
        from services import account_manager

        result = await account_manager.post_to_channel(
            acc_row["session_str"], channel_id, text, access_hash=access_hash
        )
        if "msg_id" in result:
            return f"✅ Пост опубликован! ID сообщения: {result['msg_id']}"
        return f"❌ Ошибка публикации: {result.get('error', 'неизвестная ошибка')}"

    elif name == "create_channel":
        acc_id = action_data["acc_id"]
        title = action_data["title"]
        about = action_data.get("about", "")
        username = action_data.get("username", "")
        is_group = action_data.get("is_group", False)
        acc_row = await pool.fetchrow(
            "SELECT session_str, device_model, system_version, app_version, phone "
            "FROM tg_accounts WHERE owner_id=$1 AND id=$2 AND is_active=TRUE",
            user_id,
            acc_id,
        )
        if not acc_row:
            return "❌ Аккаунт не найден или не активен"
        from services import account_manager

        if is_group:
            result = await account_manager.create_group(
                acc_row["session_str"], title, about=about, _acc=dict(acc_row)
            )
        else:
            result = await account_manager.create_channel(
                acc_row["session_str"], title, about=about, _acc=dict(acc_row)
            )
        if isinstance(result, dict) and result.get("id"):
            ch_id = result["id"]
            # Save to managed_channels
            await pool.execute(
                """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT(owner_id, channel_id) DO UPDATE SET title=$4""",
                user_id,
                acc_id,
                ch_id,
                title,
                username or None,
            )
            # Set username if provided
            if username:
                err = await account_manager.set_channel_username(
                    acc_row["session_str"], ch_id, username, _acc=dict(acc_row)
                )
                if err:
                    return f"✅ {'Группа' if is_group else 'Канал'} «{title}» создан (ID: {ch_id})\n⚠️ Username не удалось установить: {err}"
                return f"✅ {'Группа' if is_group else 'Канал'} «{title}» создан! ID: {ch_id}, @{username}"
            return (
                f"✅ {'Группа' if is_group else 'Канал'} «{title}» создан! ID: {ch_id}"
            )
        err_msg = result if isinstance(result, str) else str(result)
        return f"❌ Ошибка создания: {err_msg[:200]}"

    elif name == "create_bot":
        acc_id = action_data["acc_id"]
        bot_name = action_data["name"]
        bot_username = action_data.get("username", "")
        description = action_data.get("description", "")
        acc_row = await pool.fetchrow(
            "SELECT session_str, device_model, system_version, app_version, phone "
            "FROM tg_accounts WHERE owner_id=$1 AND id=$2 AND is_active=TRUE",
            user_id,
            acc_id,
        )
        if not acc_row:
            return "❌ Аккаунт не найден или не активен"
        # Ensure username ends with _bot
        if bot_username and not bot_username.lower().endswith("bot"):
            bot_username = bot_username + "_bot"
        from services import account_manager

        result = await account_manager.create_bot_via_botfather(
            acc_row["session_str"],
            bot_name,
            bot_username or "ai_bot",
            _acc=dict(acc_row),
        )
        if result.get("error"):
            return f"❌ Ошибка создания бота: {result['error'][:200]}"
        token = result.get("token", "")
        actual_username = result.get("username", bot_username)
        # Save to managed_bots
        try:
            if token and ":" in token:
                bot_id_int = int(token.split(":")[0])
                from database import db as _db

                await _db.add_bot(
                    pool, token, bot_id_int, actual_username, bot_name, user_id
                )
        except Exception as e:
            log.warning("ai_tools create_bot: managed_bots insert failed: %s", e)
        # Set description if provided
        if description and token and http:
            from services import bot_api

            await bot_api.set_description(http, token, description)
            await bot_api.set_short_description(http, token, description[:120])
        return f"✅ Бот @{actual_username} создан!\nТокен сохранён в системе.\nID: {token.split(':')[0] if ':' in token else '?'}"

    elif name == "create_group":
        acc_id = action_data["acc_id"]
        title = action_data["title"]
        about = action_data.get("about", "")
        username = action_data.get("username", "")
        acc_row = await pool.fetchrow(
            "SELECT session_str, device_model, system_version, app_version, phone "
            "FROM tg_accounts WHERE owner_id=$1 AND id=$2 AND is_active=TRUE",
            user_id,
            acc_id,
        )
        if not acc_row:
            return "❌ Аккаунт не найден или не активен"
        from services import account_manager

        result = await account_manager.create_group(
            acc_row["session_str"], title, about=about, _acc=dict(acc_row)
        )
        if isinstance(result, dict) and result.get("id"):
            ch_id = result["id"]
            # Save to managed_channels
            await pool.execute(
                """INSERT INTO managed_channels(owner_id, acc_id, channel_id, title, username)
                   VALUES($1,$2,$3,$4,$5)
                   ON CONFLICT(owner_id, channel_id) DO UPDATE SET title=$4""",
                user_id,
                acc_id,
                ch_id,
                title,
                username or None,
            )
            if username:
                err = await account_manager.set_channel_username(
                    acc_row["session_str"], ch_id, username, _acc=dict(acc_row)
                )
                if err:
                    return f"✅ Группа «{title}» создана (ID: {ch_id})\n⚠️ Username не удалось установить: {err}"
                return f"✅ Группа «{title}» создана! ID: {ch_id}, @{username}"
            return f"✅ Группа «{title}» создана! ID: {ch_id}"
        err_msg = result if isinstance(result, str) else str(result)
        return f"❌ Ошибка создания группы: {err_msg[:200]}"

    elif name == "bulk_create_channels":
        # Enqueue in operation_queue instead of immediate execution
        count = int(action_data.get("count", 5))
        op_id = await pool.fetchval(
            """INSERT INTO operation_queue(owner_id, op_type, status, params, total_items, done_items)
               VALUES($1, 'bulk_create_channels', 'pending', $2::jsonb, $3, 0)
               RETURNING id""",
            user_id,
            json.dumps(action_data),
            count,
        )
        return (
            f"✅ Операция #{op_id} поставлена в очередь.\n"
            f"Будет создано {count} каналов с префиксом «{action_data.get('prefix', '?')}».\n"
            f"Следите за прогрессом в 📋 Очередь операций."
        )

    elif name == "schedule_broadcast":
        import datetime

        bot_id = action_data["bot_id"]
        text = action_data["text"]
        when_minutes = int(action_data.get("when_minutes", 60))
        scheduled_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            minutes=when_minutes
        )
        row = await pool.fetchrow(
            "SELECT bot_id FROM managed_bots WHERE bot_id=$1 AND added_by=$2 AND is_active=TRUE",
            bot_id,
            user_id,
        )
        if not row:
            return "❌ Бот не найден"
        sched_id = await pool.fetchval(
            """INSERT INTO scheduled_broadcasts(bot_id, owner_id, text, scheduled_at)
               VALUES($1,$2,$3,$4) RETURNING id""",
            bot_id,
            user_id,
            text,
            scheduled_at,
        )
        return f"✅ Рассылка запланирована на {scheduled_at.strftime('%d.%m %H:%M')} UTC (через {when_minutes} мин). ID: {sched_id}"

    return "❌ Неизвестное действие"


# ── TOOL DEFINITIONS ──────────────────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "get_my_bots",
        "description": "Получить список всех ботов пользователя с базовой статистикой (аудитория, swarm, кластер)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_bot_details",
        "description": "Получить детальную информацию о конкретном боте по его ID",
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id": {
                    "type": "integer",
                    "description": "Числовой ID бота в Telegram",
                }
            },
            "required": ["bot_id"],
        },
    },
    {
        "name": "get_network_stats",
        "description": "Получить сводную статистику по всем ботам пользователя",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_audience_activity",
        "description": "Получить сегментацию аудитории бота: горячие/тёплые/холодные/потерянные",
        "input_schema": {
            "type": "object",
            "properties": {"bot_id": {"type": "integer"}},
            "required": ["bot_id"],
        },
    },
    {
        "name": "get_growth_trend",
        "description": "Получить динамику роста аудитории бота за N дней",
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "integer"},
                "days": {
                    "type": "integer",
                    "description": "Кол-во дней (по умолчанию 7)",
                    "default": 7,
                },
            },
            "required": ["bot_id"],
        },
    },
    {
        "name": "get_seo_recommendations",
        "description": "Получить SEO-оценку и рекомендации по профилю бота",
        "input_schema": {
            "type": "object",
            "properties": {"bot_id": {"type": "integer"}},
            "required": ["bot_id"],
        },
    },
    {
        "name": "get_my_accounts",
        "description": "Получить список подключённых Telegram-аккаунтов пользователя",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_my_channels",
        "description": "Получить список каналов из кэша (нужно сначала открыть «Мои каналы» для обновления кэша)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_broadcast_history",
        "description": "Получить историю рассылок для конкретного бота",
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "integer"},
                "limit": {
                    "type": "integer",
                    "description": "Кол-во последних рассылок (по умолчанию 10)",
                    "default": 10,
                },
            },
            "required": ["bot_id"],
        },
    },
    {
        "name": "search_memory",
        "description": "Найти сохранённую память BotMother пользователя по ключевым словам. Используй перед ответом, если вопрос связан с прошлым контекстом, настройками, проектами, ошибками или предпочтениями.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Что искать в памяти"},
                "limit": {"type": "integer", "default": 8},
            },
        },
    },
    {
        "name": "remember",
        "description": "Сохранить важную память BotMother. Используй только когда пользователь явно просит запомнить, сохранить правило, проект, настройку, вывод или устойчивый факт.",
        "input_schema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "Что сохранить"},
                "title": {"type": "string", "description": "Короткий заголовок"},
                "kind": {"type": "string", "default": "note"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Короткие теги без #",
                },
                "pinned": {"type": "boolean", "default": False},
            },
            "required": ["body"],
        },
    },
    {
        "name": "launch_broadcast",
        "description": (
            "ДЕЙСТВИЕ: Запустить рассылку для всех активных пользователей бота. "
            "Требует подтверждения пользователя перед выполнением. "
            "Используй когда пользователь явно просит запустить рассылку/отправить сообщение аудитории."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "integer", "description": "ID бота для рассылки"},
                "text": {
                    "type": "string",
                    "description": "Текст сообщения (HTML поддерживается)",
                },
            },
            "required": ["bot_id", "text"],
        },
    },
    {
        "name": "update_bot_profile",
        "description": (
            "ДЕЙСТВИЕ: Обновить профиль бота (имя, описание, краткое описание). "
            "Требует подтверждения пользователя перед выполнением. "
            "Укажи только те поля, которые нужно изменить."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "integer"},
                "name": {"type": "string", "description": "Новое имя бота"},
                "description": {
                    "type": "string",
                    "description": "Новое полное описание бота",
                },
                "short_description": {
                    "type": "string",
                    "description": "Новое краткое описание (about)",
                },
            },
            "required": ["bot_id"],
        },
    },
    {
        "name": "post_to_channel",
        "description": (
            "ДЕЙСТВИЕ: Опубликовать пост в канал через подключённый Telegram-аккаунт. "
            "Требует подтверждения пользователя перед выполнением. "
            "Сначала вызови get_my_channels чтобы узнать доступные каналы."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "integer",
                    "description": "Числовой ID канала из get_my_channels",
                },
                "text": {
                    "type": "string",
                    "description": "Текст поста (HTML поддерживается)",
                },
            },
            "required": ["channel_id", "text"],
        },
    },
    {
        "name": "create_channel",
        "description": (
            "ДЕЙСТВИЕ: Создать новый канал или группу в Telegram через подключённый аккаунт. "
            "Требует подтверждения пользователя. "
            "Используй для создания Telegram-присутствия: новых каналов, групп, сетей. "
            "Для группы передай is_group=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Название канала или группы",
                },
                "about": {"type": "string", "description": "Описание (опционально)"},
                "username": {
                    "type": "string",
                    "description": "Username без @ (опционально, только a-z, 0-9, _)",
                },
                "is_group": {
                    "type": "boolean",
                    "description": "true = создать группу, false = канал",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "schedule_broadcast",
        "description": (
            "ДЕЙСТВИЕ: Запланировать рассылку для бота на заданное время. "
            "Требует подтверждения пользователя. "
            "when_minutes — через сколько минут запустить рассылку."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bot_id": {"type": "integer", "description": "ID бота"},
                "text": {"type": "string", "description": "Текст рассылки"},
                "when_minutes": {
                    "type": "integer",
                    "description": "Через сколько минут запустить (по умолчанию 60)",
                    "default": 60,
                },
            },
            "required": ["bot_id", "text"],
        },
    },
    {
        "name": "create_bot",
        "description": (
            "ДЕЙСТВИЕ: Создать нового бота через BotFather. "
            "Требует подтверждения пользователя. "
            "Используй когда пользователь просит создать бота с определённым именем."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Имя бота (отображаемое)"},
                "username": {
                    "type": "string",
                    "description": "Username бота без @ (опционально, закончится на _bot)",
                },
                "description": {
                    "type": "string",
                    "description": "Описание бота (опционально)",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "create_group",
        "description": (
            "ДЕЙСТВИЕ: Создать группу/супергруппу в Telegram. "
            "Требует подтверждения пользователя. "
            "Используй когда пользователь просит создать группу (НЕ канал). "
            "Для канала используй create_channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Название группы"},
                "about": {
                    "type": "string",
                    "description": "Описание группы (опционально)",
                },
                "username": {
                    "type": "string",
                    "description": "Username без @ (опционально)",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "bulk_create_channels",
        "description": (
            "ДЕЙСТВИЕ: Массовое создание каналов (3-50 шт) с умными анти-бан задержками. "
            "Операция ставится в фоновую очередь — не блокирует интерфейс. "
            "Требует подтверждения пользователя. "
            "Используй когда пользователь просит создать НЕСКОЛЬКО каналов."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prefix": {
                    "type": "string",
                    "description": "Префикс названия (например «Новости Москва» → каналы «Новости Москва #1», ...)",
                },
                "count": {
                    "type": "integer",
                    "description": "Количество каналов (1-50, по умолчанию 5)",
                    "default": 5,
                },
                "about": {
                    "type": "string",
                    "description": "Описание для всех каналов (опционально)",
                },
                "username_pattern": {
                    "type": "string",
                    "description": "Шаблон username без @ (опционально, например news_msk → news_msk_1, ...)",
                },
                "acc_id": {
                    "type": "integer",
                    "description": "ID аккаунта (опционально, по умолчанию — лучший по trust_score)",
                },
            },
            "required": ["prefix", "count"],
        },
    },
]


async def run_tool(
    name: str, inputs: dict, pool: asyncpg.Pool, user_id: int, http=None
) -> str:
    """Execute a tool and return JSON string result."""
    try:
        if name == "get_my_bots":
            result = await get_my_bots(pool, user_id)
        elif name == "get_bot_details":
            result = await get_bot_details(pool, user_id, inputs["bot_id"])
        elif name == "get_network_stats":
            result = await get_network_stats(pool, user_id)
        elif name == "get_audience_activity":
            result = await get_audience_activity(pool, user_id, inputs["bot_id"])
        elif name == "get_growth_trend":
            result = await get_growth_trend(
                pool, user_id, inputs["bot_id"], inputs.get("days", 7)
            )
        elif name == "get_seo_recommendations":
            result = await get_seo_recommendations(pool, user_id, inputs["bot_id"])
        elif name == "get_my_accounts":
            result = await get_my_accounts(pool, user_id)
        elif name == "get_my_channels":
            result = await get_my_channels(pool, user_id)
        elif name == "get_broadcast_history":
            result = await get_broadcast_history(
                pool, user_id, inputs["bot_id"], inputs.get("limit", 10)
            )
        elif name == "search_memory":
            from services import ai_memory

            try:
                limit = int(inputs.get("limit", 8) or 8)
            except (TypeError, ValueError):
                limit = 8
            limit = max(1, min(limit, 20))
            items = await ai_memory.search(
                pool,
                user_id,
                inputs.get("query", ""),
                limit=limit,
            )
            result = {
                "items": [
                    {
                        "id": item.id,
                        "kind": item.kind,
                        "title": item.title,
                        "body": item.body,
                        "tags": item.tags,
                        "pinned": item.pinned,
                    }
                    for item in items
                ]
            }
        elif name == "remember":
            from services import ai_memory

            item = await ai_memory.remember(
                pool,
                user_id,
                inputs["body"],
                title=inputs.get("title", ""),
                kind=inputs.get("kind", "note"),
                tags=inputs.get("tags", []),
                source="ai_tool",
                pinned=bool(inputs.get("pinned", False)),
            )
            result = {"saved": True, "id": item.id, "title": item.title}
        elif name == "launch_broadcast":
            result = await action_launch_broadcast(
                pool, user_id, inputs["bot_id"], inputs["text"]
            )
        elif name == "update_bot_profile":
            result = await action_update_bot_profile(
                pool,
                user_id,
                inputs["bot_id"],
                name=inputs.get("name"),
                description=inputs.get("description"),
                short_description=inputs.get("short_description"),
            )
        elif name == "post_to_channel":
            result = await action_post_to_channel(
                pool, user_id, inputs["channel_id"], inputs["text"]
            )
        elif name == "create_channel":
            result = await action_create_channel(
                pool,
                user_id,
                title=inputs["title"],
                about=inputs.get("about", ""),
                username=inputs.get("username", ""),
                is_group=inputs.get("is_group", False),
            )
        elif name == "schedule_broadcast":
            result = await action_schedule_broadcast(
                pool,
                user_id,
                bot_id=inputs["bot_id"],
                text=inputs["text"],
                when_minutes=inputs.get("when_minutes", 60),
            )
        elif name == "create_bot":
            result = await action_create_bot(
                pool,
                user_id,
                name=inputs["name"],
                username=inputs.get("username", ""),
                description=inputs.get("description", ""),
            )
        elif name == "create_group":
            result = await action_create_group(
                pool,
                user_id,
                title=inputs["title"],
                about=inputs.get("about", ""),
                username=inputs.get("username", ""),
            )
        elif name == "bulk_create_channels":
            result = await action_bulk_create_channels(
                pool,
                user_id,
                prefix=inputs["prefix"],
                count=inputs.get("count", 5),
                about=inputs.get("about", ""),
                username_pattern=inputs.get("username_pattern", ""),
                acc_id=inputs.get("acc_id", 0),
            )
        else:
            result = {"error": f"Unknown tool: {name}"}
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})

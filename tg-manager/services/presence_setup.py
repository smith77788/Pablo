"""Presence Pack Setup Service — seed posts, bot promotion, cross-linking."""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import aiohttp
import asyncpg

log = logging.getLogger(__name__)


def generate_admin_token() -> str:
    return secrets.token_urlsafe(16)


def build_seed_post(
    channel_title: str,
    bot_username: str | None = None,
    group_link: str | None = None,
    target_url: str | None = None,
    target_label: str | None = None,
    pack_description: str | None = None,
) -> str:
    """Generate an initial cross-linked post for a channel."""
    lines = [f"<b>{channel_title}</b>\n"]

    if pack_description:
        lines.append(f"{pack_description}\n")

    has_links = bot_username or group_link or target_url
    if has_links:
        lines.append("\n🔗 <b>Полезные ссылки:</b>")

    if bot_username:
        clean = bot_username.lstrip("@")
        lines.append(f"🤖 Бот: @{clean}")

    if group_link:
        lines.append(f"💬 Чат: {group_link}")

    if target_url:
        label = target_label or "Главный ресурс"
        lines.append(f"🎯 {label}: {target_url}")

    lines.append("\n<i>Подпишитесь, чтобы не пропустить важное!</i>")
    return "\n".join(lines)


def build_group_welcome(
    group_title: str,
    bot_username: str | None = None,
    channel_link: str | None = None,
    target_url: str | None = None,
    target_label: str | None = None,
) -> str:
    """Generate a welcome message for a group."""
    lines = [f"👋 Добро пожаловать в <b>{group_title}</b>!\n"]
    lines.append("Рады видеть вас в нашем сообществе.\n")

    has_links = bot_username or channel_link or target_url
    if has_links:
        lines.append("🔗 <b>Важные ссылки:</b>")

    if channel_link:
        lines.append(f"📡 Наш канал: {channel_link}")

    if bot_username:
        clean = bot_username.lstrip("@")
        lines.append(f"🤖 Наш бот: @{clean}")

    if target_url:
        label = target_label or "Главный ресурс"
        lines.append(f"🎯 {label}: {target_url}")

    lines.append("\n<i>Пожалуйста, соблюдайте правила чата. Будьте вежливы!</i>")
    return "\n".join(lines)


async def seed_channel_post(
    http: aiohttp.ClientSession,
    bot_token: str,
    channel_id: int | str,
    text: str,
) -> bool:
    """Post a message to a channel via Bot API. Bot must be admin in the channel."""
    from services import bot_api
    try:
        result = await bot_api.send_message(http, bot_token, channel_id, text)
        return bool(result)
    except Exception as e:
        log.warning("seed_channel_post failed for %s: %s", channel_id, e)
        return False


async def seed_channel_via_account(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
    access_hash: int,
    text: str,
) -> bool:
    """Post via userbot account when bot is not yet admin in channel."""
    from services import account_manager
    acc = await pool.fetchrow(
        "SELECT id,session_str,device_model,system_version,app_version "
        "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "AND (cooldown_until IS NULL OR cooldown_until < now()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
        owner_id,
    )
    if not acc:
        return False
    try:
        result = await account_manager.post_to_channel(
            acc["session_str"], channel_id,
            text, access_hash=access_hash or 0, _acc=dict(acc),
        )
        return bool(result.get("id")) if isinstance(result, dict) else bool(result)
    except Exception as e:
        log.warning("seed_channel_via_account failed for %s: %s", channel_id, e)
        return False


async def promote_bot_in_channel(
    pool: asyncpg.Pool,
    owner_id: int,
    channel_id: int,
    access_hash: int,
    bot_tg_id: int,
) -> bool:
    """Add a bot as admin (post_messages, invite_users) in a channel via userbot."""
    from services import account_manager
    acc = await pool.fetchrow(
        "SELECT id,session_str,device_model,system_version,app_version "
        "FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
        "AND (cooldown_until IS NULL OR cooldown_until < now()) "
        "ORDER BY trust_score DESC NULLS LAST LIMIT 1",
        owner_id,
    )
    if not acc:
        log.warning("promote_bot_in_channel: no active account for owner %s", owner_id)
        return False
    try:
        ok = await account_manager.promote_to_admin(
            acc["session_str"],
            channel_id,
            bot_tg_id,
            _acc=dict(acc),
            access_hash=access_hash or 0,
            post_messages=True,
            invite_users=True,
            change_info=False,
        )
        return ok
    except Exception as e:
        log.warning("promote_bot_in_channel error chan=%s bot=%s: %s", channel_id, bot_tg_id, e)
        return False


async def mirror_sync_auto_replies(
    pool: asyncpg.Pool,
    source_bot_id: int,
    owner_id: int,
) -> tuple[int, int]:
    """Copy auto_replies from source_bot to all bots in the same cluster.

    Returns (synced_count, total_mirrors).
    """
    from database import db
    # Get source bot cluster
    source_bot = await pool.fetchrow(
        "SELECT cluster FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        source_bot_id, owner_id,
    )
    if not source_bot or not source_bot["cluster"]:
        return 0, 0

    cluster = source_bot["cluster"]

    # Get all bots in same cluster (excluding source)
    mirrors = await pool.fetch(
        "SELECT bot_id FROM managed_bots WHERE added_by=$1 AND cluster=$2 AND bot_id!=$3 AND is_active=TRUE",
        owner_id, cluster, source_bot_id,
    )
    if not mirrors:
        return 0, 0

    total = len(mirrors)
    synced = 0
    for m in mirrors:
        try:
            copied = await db.copy_auto_replies(pool, source_bot_id, m["bot_id"])
            if copied >= 0:
                synced += 1
        except Exception as e:
            log.warning("mirror_sync_auto_replies: failed for bot %s: %s", m["bot_id"], e)

    return synced, total

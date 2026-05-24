"""Background auto-reply polling service."""
from __future__ import annotations
import asyncio
import logging
import aiohttp
import asyncpg
from database import db
from services import bot_api
from services import routing_engine

log = logging.getLogger(__name__)


def _match_rule(rule: dict, text: str) -> bool:
    if not text:
        return False
    t = rule["trigger_type"]
    if t == "start":
        return text.strip().lower().startswith("/start")
    if t == "keyword":
        return rule["keyword"].lower() in text.lower()
    if t == "any":
        return True
    return False


async def _init_offset(pool: asyncpg.Pool, http: aiohttp.ClientSession,
                       bot_id: int, token: str) -> int:
    """On first run: skip all pending updates, store current max_id as start point."""
    data = await bot_api._call(http, token, "getUpdates", offset=-1, limit=1, timeout=0)
    updates = data.get("result", []) if data.get("ok") else []
    if updates:
        max_id = updates[-1]["update_id"]
        await db.set_update_offset(pool, bot_id, max_id)
        return max_id
    return 0


async def _process_bot(pool: asyncpg.Pool, http: aiohttp.ClientSession,
                       bot_id: int, token: str) -> None:
    try:
        offset = await db.get_update_offset(pool, bot_id)
        if offset == 0:
            await _init_offset(pool, http, bot_id, token)
            return
        data = await bot_api._call(http, token, "getUpdates",
                                   offset=offset + 1,
                                   limit=100, timeout=0)
        updates = data.get("result", []) if data.get("ok") else []
        if not updates:
            return

        # Fetch per-bot data ONCE, outside the per-message loop
        rules = await db.get_active_auto_replies(pool, bot_id)
        funnels = await db.get_active_funnels(pool, bot_id)
        automation_rules = await db.get_active_automation_rules(pool, bot_id)
        bot_row = await pool.fetchrow(
            "SELECT bot_role, swarm_enabled, cluster FROM managed_bots WHERE bot_id=$1",
            bot_id,
        )
        active_exp = await db.get_active_experiment(pool, bot_id, "start_message")

        max_update_id = offset

        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid > max_update_id:
                max_update_id = uid

            msg = upd.get("message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if not chat_id or not text:
                continue

            is_start = text.strip().lower().startswith("/start")

            # Track user activity
            await db.upsert_user_activity(pool, bot_id, chat_id)

            # Deep link tracking: /start <param>
            if text.strip().lower().startswith("/start "):
                parts = text.strip().split(None, 1)
                if len(parts) == 2:
                    param = parts[1].strip()
                    link_id = await db.record_deep_link_visit(pool, bot_id, param, chat_id)
                    if param.startswith("ref") and param[3:].isdigit():
                        referrer_id = int(param[3:])
                        if referrer_id != chat_id:
                            await db.record_referral(pool, bot_id, referrer_id, chat_id, link_id)

            # Track non-command keywords for SEO analytics
            if not text.startswith("/"):
                await db.record_message_keywords(pool, bot_id, text)

            # Auto-replies (first match wins)
            for rule in rules:
                if _match_rule(rule, text):
                    await bot_api.send_message(http, token, chat_id, rule["response_text"])
                    break

            # Swarm routing: /start on entry bot with swarm enabled
            if is_start and bot_row and bot_row["swarm_enabled"] and bot_row["bot_role"] == "entry":
                await routing_engine.make_routing_decision(
                    pool, http, bot_id, chat_id, chat_id, token,
                    bot_row["cluster"] or "default",
                )

            # Automation rules
            for arule in automation_rules:
                triggered = False
                if arule["trigger_type"] == "message_received":
                    triggered = True
                elif arule["trigger_type"] == "keyword" and arule.get("trigger_value"):
                    triggered = arule["trigger_value"].lower() in text.lower()
                # user_joined handled elsewhere; tag_added handled in CRM

                if triggered:
                    if arule["action_type"] == "send_message":
                        await bot_api.send_message(http, token, chat_id, arule["action_value"])
                    elif arule["action_type"] == "add_tag":
                        await db.add_user_tag(pool, bot_id, chat_id, arule["action_value"])
                    elif arule["action_type"] == "remove_tag":
                        await db.remove_user_tag(pool, bot_id, chat_id, arule["action_value"])

            # Funnels: subscribe on /start or keyword
            for funnel in funnels:
                if funnel["trigger_type"] == "start" and is_start:
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)
                elif (funnel["trigger_type"] == "keyword" and funnel["keyword"]
                      and funnel["keyword"].lower() in text.lower()):
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)

            # A/B experiment: assign variant on /start and SEND the variant content
            if is_start and active_exp:
                variant = await db.assign_experiment_variant(pool, bot_id, chat_id, active_exp["id"])
                if variant and variant.get("content"):
                    await bot_api.send_message(http, token, chat_id, variant["content"])

        if max_update_id > offset:
            await db.set_update_offset(pool, bot_id, max_update_id)

    except Exception:
        log.exception("Auto-responder error for bot %d", bot_id)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    while True:
        try:
            bots = await db.get_bots_for_polling(pool)
            if bots:
                await asyncio.gather(
                    *(_process_bot(pool, http, b["bot_id"], b["token"]) for b in bots),
                    return_exceptions=True,
                )
        except Exception:
            log.exception("Auto-responder loop error")
        await asyncio.sleep(30)

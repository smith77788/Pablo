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
    else:
        max_id = 1  # sentinel: no pending updates, mark as initialized
    await db.set_update_offset(pool, bot_id, max_id)
    return max_id


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

            # Track user activity — returns True for first-ever message (new user)
            is_new_user = await db.upsert_user_activity(pool, bot_id, chat_id)

            # Register in bot_users so the user appears in broadcast audience
            from_user = msg.get("from") or {}
            await db.upsert_users(pool, bot_id, [{
                "user_id": chat_id,
                "username": from_user.get("username", ""),
                "first_name": from_user.get("first_name", ""),
                "last_name": from_user.get("last_name", ""),
                "language_code": from_user.get("language_code", ""),
            }])

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
            newly_added_tags: list[str] = []
            for arule in automation_rules:
                triggered = False
                if arule["trigger_type"] == "message_received":
                    triggered = True
                elif arule["trigger_type"] == "keyword" and arule.get("trigger_value"):
                    triggered = arule["trigger_value"].lower() in text.lower()
                elif arule["trigger_type"] == "user_joined" and is_new_user:
                    triggered = True

                if triggered:
                    if arule["action_type"] == "send_message":
                        await bot_api.send_message(http, token, chat_id, arule["action_value"])
                    elif arule["action_type"] == "add_tag":
                        await db.add_user_tag(pool, bot_id, chat_id, arule["action_value"])
                        newly_added_tags.append(arule["action_value"])
                    elif arule["action_type"] == "remove_tag":
                        await db.remove_user_tag(pool, bot_id, chat_id, arule["action_value"])
                    elif arule["action_type"] == "subscribe_funnel":
                        try:
                            await db.subscribe_to_funnel(pool, int(arule["action_value"]), chat_id)
                        except (ValueError, TypeError):
                            pass
                    elif arule["action_type"] == "webhook":
                        # action_value = URL to POST to
                        url = arule.get("action_value", "").strip()
                        if url:
                            try:
                                payload = {
                                    "bot_id": bot_id,
                                    "chat_id": chat_id,
                                    "trigger_type": arule["trigger_type"],
                                    "trigger_value": arule.get("trigger_value"),
                                    "user": {
                                        "id": chat_id,
                                        "username": from_user.get("username", ""),
                                        "first_name": from_user.get("first_name", ""),
                                    },
                                    "text": text,
                                }
                                async with http.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                    log.debug("webhook action: url=%s status=%s", url, resp.status)
                            except Exception as exc:
                                log.warning("webhook action failed: url=%s error=%s", url, exc)
                    elif arule["action_type"] == "send_ai_reply":
                        # action_value = system prompt / persona description
                        system_prompt = arule.get("action_value") or "Ты полезный ассистент."
                        try:
                            from config import OPENAI_API_KEY
                            if OPENAI_API_KEY:
                                ai_payload = {
                                    "model": "gpt-4o-mini",
                                    "messages": [
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": text},
                                    ],
                                    "max_tokens": 300,
                                }
                                headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
                                async with http.post(
                                    "https://api.openai.com/v1/chat/completions",
                                    json=ai_payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as resp:
                                    if resp.status == 200:
                                        ai_data = await resp.json()
                                        ai_text = ai_data["choices"][0]["message"]["content"].strip()
                                        await bot_api.send_message(http, token, chat_id, ai_text)
                        except Exception as exc:
                            log.warning("send_ai_reply failed: %s", exc)

            # tag_added rules: fire once for each tag added above (one level, no recursion)
            for arule in automation_rules:
                if (arule["trigger_type"] == "tag_added"
                        and arule.get("trigger_value")
                        and arule["trigger_value"] in newly_added_tags):
                    if arule["action_type"] == "send_message":
                        await bot_api.send_message(http, token, chat_id, arule["action_value"])
                    elif arule["action_type"] == "add_tag":
                        await db.add_user_tag(pool, bot_id, chat_id, arule["action_value"])
                    elif arule["action_type"] == "remove_tag":
                        await db.remove_user_tag(pool, bot_id, chat_id, arule["action_value"])
                    elif arule["action_type"] == "subscribe_funnel":
                        try:
                            await db.subscribe_to_funnel(pool, int(arule["action_value"]), chat_id)
                        except (ValueError, TypeError):
                            pass
                    elif arule["action_type"] == "webhook":
                        url = arule.get("action_value", "").strip()
                        if url:
                            try:
                                payload = {
                                    "bot_id": bot_id,
                                    "chat_id": chat_id,
                                    "trigger_type": arule["trigger_type"],
                                    "trigger_value": arule.get("trigger_value"),
                                    "user": {
                                        "id": chat_id,
                                        "username": from_user.get("username", ""),
                                        "first_name": from_user.get("first_name", ""),
                                    },
                                    "text": text,
                                }
                                async with http.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                    log.debug("webhook action: url=%s status=%s", url, resp.status)
                            except Exception as exc:
                                log.warning("webhook action failed: url=%s error=%s", url, exc)
                    elif arule["action_type"] == "send_ai_reply":
                        system_prompt = arule.get("action_value") or "Ты полезный ассистент."
                        try:
                            from config import OPENAI_API_KEY
                            if OPENAI_API_KEY:
                                ai_payload = {
                                    "model": "gpt-4o-mini",
                                    "messages": [
                                        {"role": "system", "content": system_prompt},
                                        {"role": "user", "content": text},
                                    ],
                                    "max_tokens": 300,
                                }
                                headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
                                async with http.post(
                                    "https://api.openai.com/v1/chat/completions",
                                    json=ai_payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as resp:
                                    if resp.status == 200:
                                        ai_data = await resp.json()
                                        ai_text = ai_data["choices"][0]["message"]["content"].strip()
                                        await bot_api.send_message(http, token, chat_id, ai_text)
                        except Exception as exc:
                            log.warning("send_ai_reply failed: %s", exc)

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
            elif not is_start and active_exp:
                # Conversion: any subsequent message from an assigned user counts
                try:
                    await db.record_experiment_conversion(pool, bot_id, chat_id, active_exp["id"])
                except Exception:
                    pass

        if max_update_id > offset:
            await db.set_update_offset(pool, bot_id, max_update_id)

    except Exception:
        log.exception("Auto-responder error for bot %d", bot_id)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    asyncio.get_event_loop().create_task(run_inactivity_sweep(pool, http))
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


async def run_inactivity_sweep(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Background sweep: fire inactivity automation rules."""
    await asyncio.sleep(600)  # startup delay 10 min
    while True:
        try:
            await _inactivity_sweep(pool, http)
        except Exception:
            log.exception("inactivity_sweep error")
        await asyncio.sleep(3600)  # hourly


async def _inactivity_sweep(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Find users inactive for N days and fire matching rules."""
    # Get all active inactivity rules
    rules = await pool.fetch(
        """SELECT ar.*, mb.token
           FROM automation_rules ar
           JOIN managed_bots mb ON mb.bot_id = ar.bot_id
           WHERE ar.trigger_type = 'inactivity'
             AND ar.is_active = true
             AND mb.is_active = true"""
    )
    if not rules:
        return

    for rule in rules:
        try:
            inactivity_days = int(rule["trigger_value"] or "3")
        except (ValueError, TypeError):
            inactivity_days = 3

        # Find users inactive for N days (use user_activity table)
        inactive_users = await pool.fetch(
            """SELECT ua.chat_id FROM user_activity ua
               WHERE ua.bot_id = $1
                 AND ua.last_seen < NOW() - ($2 * INTERVAL '1 day')
                 AND NOT EXISTS (
                     SELECT 1 FROM inactivity_alerts_sent ias
                     WHERE ias.bot_id = $1
                       AND ias.chat_id = ua.chat_id
                       AND ias.rule_id = $3
                       AND ias.sent_at > NOW() - ($2 * INTERVAL '1 day')
                 )
               LIMIT 100""",
            rule["bot_id"], inactivity_days, rule["id"],
        )

        for user in inactive_users:
            chat_id = user["chat_id"]
            try:
                if rule["action_type"] == "send_message":
                    await bot_api.send_message(http, rule["token"], chat_id, rule["action_value"])
                elif rule["action_type"] == "webhook":
                    url = (rule["action_value"] or "").strip()
                    if url:
                        await http.post(url, json={
                            "bot_id": rule["bot_id"], "chat_id": chat_id,
                            "trigger_type": "inactivity", "inactivity_days": inactivity_days,
                        }, timeout=aiohttp.ClientTimeout(total=10))

                # Mark as sent (prevent duplicate)
                await pool.execute(
                    """INSERT INTO inactivity_alerts_sent(bot_id, chat_id, rule_id)
                       VALUES($1, $2, $3)
                       ON CONFLICT(bot_id, chat_id, rule_id) DO UPDATE SET sent_at=NOW()""",
                    rule["bot_id"], chat_id, rule["id"],
                )
                await asyncio.sleep(0.1)  # tiny delay between messages
            except Exception as exc:
                log.warning("inactivity_sweep: failed for chat=%s: %s", chat_id, exc)

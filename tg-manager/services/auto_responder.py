"""Background auto-reply polling service."""

from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime

import aiohttp
import asyncpg
from database import db
from services import bot_api
from services import brand_injection
from services import routing_engine
from services.logger import log_exc_swallow
from bot.utils.template_validator import replace_placeholders

log = logging.getLogger(__name__)

# Rate limiter: prevent one user from triggering too many automation rules at once.
# Maps (bot_id, chat_id) → count of rules fired in the current polling cycle.
# Reset per-cycle in _process_bot.
_MAX_RULES_PER_USER_PER_CYCLE = 5
# Tracks rule execution counts within a single _process_bot call.
# Structure: {(bot_id, chat_id): int}
_cycle_rule_counts: dict[tuple[int, int], int] = {}


def _render_text(text: str, from_user: dict, bot_row: dict | None = None) -> str:
    """Render {{PLACEHOLDER}} tokens in text with user/bot context."""
    if not text or "{{" not in text:
        return text
    username = from_user.get("username", "") or ""
    first_name = from_user.get("first_name", "") or ""
    last_name = from_user.get("last_name", "") or ""
    bot_name = (
        (bot_row.get("username") or bot_row.get("first_name") or "") if bot_row else ""
    )
    now = datetime.now()
    return replace_placeholders(
        text,
        {
            "USERNAME": f"@{username}" if username else first_name,
            "FIRST_NAME": first_name,
            "LAST_NAME": last_name,
            "FULL_NAME": f"{first_name} {last_name}".strip(),
            "BOT_NAME": bot_name,
            "DATE": now.strftime("%d.%m.%Y"),
            "DATE_SHORT": now.strftime("%d.%m"),
            "TIME": now.strftime("%H:%M"),
        },
    )


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


async def _init_offset(
    pool: asyncpg.Pool, http: aiohttp.ClientSession, bot_id: int, token: str
) -> int:
    """On first run: skip all pending updates, store current max_id as start point."""
    data = await bot_api._call(http, token, "getUpdates", offset=-1, limit=1, timeout=0)
    updates = data.get("result", []) if data.get("ok") else []
    if updates:
        max_id = updates[-1]["update_id"]
    else:
        max_id = 1  # sentinel: no pending updates, mark as initialized
    await db.set_update_offset(pool, bot_id, max_id)
    return max_id


async def _process_bot(
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    bot_id: int,
    token: str,
    main_bot=None,
) -> None:
    try:
        offset = await db.get_update_offset(pool, bot_id)
        if offset == 0:
            await _init_offset(pool, http, bot_id, token)
            return
        data = await bot_api._call(
            http, token, "getUpdates", offset=offset + 1, limit=100, timeout=0
        )
        if not data.get("ok"):
            # Раньше ошибка getUpdates молча проглатывалась (updates=[]), из-за
            # чего бот переставал видеть сообщения — и новые пользователи не
            # детектировались — без единой записи в лог. Частая причина: на боте
            # активен webhook (409 Conflict) или отозван токен.
            err_desc = str(data.get("description") or data.get("error_code") or "unknown")
            if "conflict" in err_desc.lower() or "webhook" in err_desc.lower():
                # Webhook перехватывает апдейты → polling не нужен; снимаем webhook,
                # чтобы вернуть бота на polling (managed-боты работают через polling).
                try:
                    # drop_pending_updates по умолчанию false — ожидающие апдейты
                    # сохранятся и будут обработаны через polling.
                    await bot_api._call(http, token, "deleteWebhook")
                    log.warning(
                        "auto_responder: bot=%d getUpdates конфликт с webhook — webhook снят, polling восстановлен",
                        bot_id,
                    )
                except Exception:
                    log.warning("auto_responder: bot=%d webhook-конфликт, deleteWebhook не удался", bot_id)
            else:
                log.warning("auto_responder: bot=%d getUpdates вернул ошибку: %s", bot_id, err_desc[:200])
            return
        updates = data.get("result", [])
        if not updates:
            return

        # Fetch per-bot data ONCE, outside the per-message loop
        rules = await db.get_active_auto_replies(pool, bot_id)
        funnels = await db.get_active_funnels(pool, bot_id)
        automation_rules = await db.get_active_automation_rules(pool, bot_id)
        bot_row = await pool.fetchrow(
            "SELECT bot_role, swarm_enabled, cluster, added_by, username, first_name, relay_enabled "
            "FROM managed_bots WHERE bot_id=$1",
            bot_id,
        )
        active_exp = await db.get_active_experiment(pool, bot_id, "start_message")

        # Brand injection: cache free-tier status once per polling cycle
        try:
            _is_free = await brand_injection.is_free_tier(pool, bot_id)
        except Exception:
            _is_free = False

        # Reset per-cycle rate-limit counters for this bot
        keys_to_clear = [k for k in _cycle_rule_counts if k[0] == bot_id]
        for k in keys_to_clear:
            del _cycle_rule_counts[k]

        max_update_id = offset

        for upd in updates:
            uid = upd.get("update_id", 0)
            if uid > max_update_id:
                max_update_id = uid

            # Answer callback_query to dismiss the button spinner in managed bots
            cbq = upd.get("callback_query")
            if cbq:
                cbq_id = cbq.get("id")
                if cbq_id:
                    try:
                        await bot_api._call(
                            http, token, "answerCallbackQuery",
                            callback_query_id=cbq_id,
                            text="",
                        )
                    except Exception:
                        pass
                continue

            msg = upd.get("message")
            if not msg:
                continue
            chat_id = msg.get("chat", {}).get("id")
            text = msg.get("text", "")
            if not chat_id or not text:
                continue

            is_start = text.strip().lower().startswith("/start")

            # Extract user info once (used for notification + registration below)
            from_user = msg.get("from") or {}

            # Track user activity — returns True for first-ever message (new user)
            is_new_user = await db.upsert_user_activity(pool, bot_id, chat_id)

            # Notify bot owner about new user
            if is_new_user:
                log.info(
                    "new_user: bot_id=%s chat_id=%s main_bot=%s added_by=%s",
                    bot_id, chat_id, bool(main_bot), bot_row.get("added_by") if bot_row else None,
                )
            if is_new_user and main_bot and bot_row and bot_row.get("added_by"):
                owner_id = bot_row["added_by"]
                bot_name = (
                    bot_row.get("username")
                    or bot_row.get("first_name")
                    or f"id{bot_id}"
                )
                user_name = (
                    from_user.get("username")
                    or from_user.get("first_name")
                    or f"id{chat_id}"
                )
                note = f"👤 <b>Новый пользователь</b> @{user_name} подписался на @{bot_name}"
                # dedup_key = (bot, новый юзер): каждый отдельный новый подписчик
                # уведомляется, иначе кулдаун по (owner, "new_user") глушил всех
                # новых юзеров со всех ботов владельца в одном слоте.
                asyncio.create_task(
                    db.notify_if_enabled(
                        pool, main_bot, owner_id, "new_user", note,
                        dedup_key=f"{bot_id}:{chat_id}",
                    )
                )

            # Register in bot_users so the user appears in broadcast audience
            await db.upsert_users(
                pool,
                bot_id,
                [
                    {
                        "user_id": chat_id,
                        "username": from_user.get("username", ""),
                        "first_name": from_user.get("first_name", ""),
                        "last_name": from_user.get("last_name", ""),
                        "language_code": from_user.get("language_code", ""),
                    }
                ],
            )

            # Deep link tracking: /start <param>
            if text.strip().lower().startswith("/start "):
                parts = text.strip().split(None, 1)
                if len(parts) == 2:
                    param = parts[1].strip()
                    link_id = await db.record_deep_link_visit(
                        pool, bot_id, param, chat_id
                    )
                    if param.startswith("ref") and param[3:].isdigit():
                        referrer_id = int(param[3:])
                        if referrer_id != chat_id:
                            await db.record_referral(
                                pool, bot_id, referrer_id, chat_id, link_id
                            )

            # Track non-command keywords for SEO analytics
            if not text.startswith("/"):
                await db.record_message_keywords(pool, bot_id, text)

            # Bot admin panel: /admin TOKEN (owner only)
            if (
                text.strip().lower().startswith("/admin ")
                and bot_row
                and bot_row.get("added_by")
            ):
                admin_token = text.strip()[7:].strip()
                if admin_token:
                    admin_row = await db.get_bot_admin_session_by_token(
                        pool, admin_token
                    )
                    if (
                        admin_row
                        and admin_row["bot_id"] == bot_id
                        and chat_id == admin_row["owner_id"]
                    ):
                        user_count = (
                            await pool.fetchval(
                                "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", bot_id
                            )
                            or 0
                        )
                        reply_count = (
                            await pool.fetchval(
                                "SELECT COUNT(*) FROM auto_replies WHERE bot_id=$1 AND is_active=TRUE",
                                bot_id,
                            )
                            or 0
                        )
                        funnel_count = (
                            await pool.fetchval(
                                "SELECT COUNT(*) FROM funnels WHERE bot_id=$1 AND is_active=true",
                                bot_id,
                            )
                            or 0
                        )
                        panel_text = (
                            "🔧 <b>Панель управления ботом</b>\n\n"
                            f"👥 Пользователей: {user_count}\n"
                            f"💬 Авто-ответов: {reply_count}\n"
                            f"🔄 Активных воронок: {funnel_count}\n\n"
                            "📌 Управление через Infragram:\n"
                            "• Авто-ответы: Настройки → Авто-ответы\n"
                            "• Рассылка: Broadcasts\n"
                            "• Воронки: Настройки → Воронки\n"
                            "• CRM и пользователи: Inbox / Relay\n\n"
                            "<i>Авторизация подтверждена ✅</i>"
                        )
                        ok, _ = await bot_api.send_message(
                            http, token, chat_id, panel_text
                        )
                        if not ok:
                            log.warning(
                                "auto_responder: failed to send admin panel to chat %d bot %d",
                                chat_id,
                                bot_id,
                            )
                        continue  # skip normal auto_replies

            # Relay mode: skip automated responses — relay.py forwards to operator.
            # Exception: /start and /support still get a response so users know how to reach support.
            if bot_row and bot_row.get("relay_enabled"):
                _SUPPORT_TRIGGERS = ("/support", "💬 написать в поддержку")
                if is_start:
                    # Prefer operator's configured start rule; fall back to generic welcome
                    start_rules = [r for r in rules if r["trigger_type"] == "start"]
                    if start_rules:
                        rendered = _render_text(start_rules[0]["response_text"], from_user, bot_row)
                        if _is_free:
                            rendered = brand_injection.add_promo(rendered, html=True, context="broadcast")
                        await bot_api.send_message(http, token, chat_id, rendered)
                    else:
                        fname = from_user.get("first_name") or "друг"
                        bot_name = bot_row.get("username") or bot_row.get("first_name") or "бот"
                        welcome = (
                            f"👋 Привет, <b>{fname}</b>!\n\n"
                            f"Добро пожаловать в <b>@{bot_name}</b>.\n\n"
                            "Если вам нужна помощь — нажмите кнопку ниже, чтобы связаться с оператором поддержки."
                        )
                        if _is_free:
                            welcome = brand_injection.add_promo(welcome, html=True, context="broadcast")
                        rkb = {
                            "keyboard": [[{"text": "💬 Написать в поддержку"}]],
                            "resize_keyboard": True,
                            "one_time_keyboard": False,
                        }
                        await bot_api.send_message(http, token, chat_id, welcome, reply_markup=rkb)
                elif text.strip().lower() in _SUPPORT_TRIGGERS:
                    ack = (
                        "✅ <b>Запрос принят!</b>\n\n"
                        "Оператор поддержки скоро ответит вам. "
                        "Вы можете написать детали вашего вопроса прямо здесь."
                    )
                    if _is_free:
                        ack = brand_injection.add_promo(ack, html=True, context="broadcast")
                    await bot_api.send_message(http, token, chat_id, ack)
                continue

            # Auto-replies (first match wins)
            for rule in rules:
                if _match_rule(rule, text):
                    rendered = _render_text(rule["response_text"], from_user, bot_row)
                    if _is_free:
                        rendered = brand_injection.add_promo(rendered, html=True, context="broadcast")
                    ok, retry = await bot_api.send_message(
                        http, token, chat_id, rendered
                    )
                    if ok:
                        # Log the fired rule to auto_reply_log for analytics
                        try:
                            await pool.execute(
                                """INSERT INTO auto_reply_log
                                       (bot_id, chat_id, rule_id, rule_type, trigger_type, keyword)
                                   VALUES ($1, $2, $3, 'auto_reply', $4, $5)""",
                                bot_id,
                                chat_id,
                                rule.get("id"),
                                rule.get("trigger_type"),
                                rule.get("keyword"),
                            )
                        except Exception as _log_err:
                            log.debug(
                                "auto_reply_log insert failed bot=%d: %s", bot_id, _log_err
                            )
                    else:
                        log.warning(
                            "auto_responder: failed to send auto-reply to chat %d bot %d%s",
                            chat_id,
                            bot_id,
                            f" (rate-limited {retry}s)" if retry else "",
                        )
                    break

            # Passive inbox relay: forward non-/start messages to operator even when
            # relay_enabled=false.  Old bots that have custom auto-replies ("Сообщение
            # успешно отправлено") still need to deliver messages to the bot owner.
            # Uses the same relay_sessions / relay_messages tables → reply-back works.
            if (
                not is_start
                and text
                and bot_row
                and not bot_row.get("relay_enabled")
                and bot_row.get("added_by")
                and main_bot
            ):
                added_by = bot_row["added_by"]
                try:
                    uname = from_user.get("username")
                    fname = from_user.get("first_name", "")
                    lname = from_user.get("last_name", "")
                    user_label = (
                        f"@{uname}"
                        if uname
                        else (f"{fname} {lname}".strip() or f"ID:{chat_id}")
                    )
                    bname = (
                        bot_row.get("username")
                        or bot_row.get("first_name")
                        or str(bot_id)
                    )
                    fwd_text = (
                        f"📨 <b>@{bname}</b>  |  👤 {user_label}\n"
                        f"<i>ID: {chat_id}</i>\n\n"
                        f"{text}\n\n"
                        f"<i>← Reply чтобы ответить пользователю</i>"
                    )
                    session_id = await db.get_or_create_relay_session(
                        pool, bot_id, chat_id, uname, fname
                    )
                    sent = await main_bot.send_message(
                        added_by, fwd_text, parse_mode="HTML"
                    )
                    await db.save_relay_message(
                        pool,
                        session_id,
                        "in",
                        text,
                        sent.message_id if sent else None,
                    )
                except Exception:
                    log.exception(
                        "auto_responder: passive relay failed bot=%d chat=%d",
                        bot_id,
                        chat_id,
                    )

            # Swarm routing: /start on entry bot with swarm enabled
            if (
                is_start
                and bot_row
                and bot_row["swarm_enabled"]
                and bot_row["bot_role"] == "entry"
            ):
                await routing_engine.make_routing_decision(
                    pool,
                    http,
                    bot_id,
                    chat_id,
                    chat_id,
                    token,
                    bot_row["cluster"] or "default",
                )

            # Automation rules
            newly_added_tags: list[str] = []
            _rate_key = (bot_id, chat_id)
            for arule in automation_rules:
                # Rate limit: cap rules fired per user per polling cycle
                if _cycle_rule_counts.get(_rate_key, 0) >= _MAX_RULES_PER_USER_PER_CYCLE:
                    log.debug(
                        "auto_responder: rate-limit hit for bot=%d chat=%d — skipping remaining rules",
                        bot_id,
                        chat_id,
                    )
                    break

                triggered = False
                if arule["trigger_type"] == "message_received":
                    triggered = True
                elif arule["trigger_type"] == "keyword" and arule.get("trigger_value"):
                    triggered = arule["trigger_value"].lower() in text.lower()
                elif arule["trigger_type"] == "user_joined" and is_new_user:
                    triggered = True

                if triggered:
                    _cycle_rule_counts[_rate_key] = _cycle_rule_counts.get(_rate_key, 0) + 1
                    if arule["action_type"] == "send_message":
                        rendered = _render_text(
                            arule["action_value"], from_user, bot_row
                        )
                        if _is_free:
                            rendered = brand_injection.add_promo(rendered, html=True, context="broadcast")
                        ok, _ = await bot_api.send_message(
                            http, token, chat_id, rendered
                        )
                        if not ok:
                            log.warning(
                                "auto_responder: failed to send automation message to chat %d bot %d",
                                chat_id,
                                bot_id,
                            )
                    elif arule["action_type"] == "add_tag":
                        await db.add_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                        newly_added_tags.append(arule["action_value"])
                    elif arule["action_type"] == "remove_tag":
                        await db.remove_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                    elif arule["action_type"] == "subscribe_funnel":
                        try:
                            await db.subscribe_to_funnel(
                                pool, int(arule["action_value"]), chat_id
                            )
                        except (ValueError, TypeError):
                            log_exc_swallow(
                                log,
                                "Неверный funnel_id в правиле авто-ответа (блок 1)",
                                rule_id=arule.get("id"),
                                action_value=arule.get("action_value"),
                            )
                    elif arule["action_type"] == "create_deal":
                        # Create a CRM deal for this user.
                        # action_value is used as deal title prefix; falls back to "Новая заявка".
                        try:
                            title_prefix = arule.get("action_value") or "Новая заявка"
                            user_label = (
                                from_user.get("username")
                                or from_user.get("first_name")
                                or str(chat_id)
                            )
                            deal_title = f"{title_prefix} — {user_label}"
                            await pool.execute(
                                """INSERT INTO crm_deals
                                       (bot_id, user_id, title, status, created_at)
                                   VALUES ($1, $2, $3, 'new', NOW())
                                   ON CONFLICT DO NOTHING""",
                                bot_id,
                                chat_id,
                                deal_title,
                            )
                        except Exception as exc:
                            log.warning(
                                "auto_responder: create_deal failed bot=%d chat=%d: %s",
                                bot_id,
                                chat_id,
                                exc,
                            )
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
                                async with http.post(
                                    url,
                                    json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10),
                                ) as resp:
                                    log.debug(
                                        "webhook action: url=%s status=%s",
                                        url,
                                        resp.status,
                                    )
                            except Exception as exc:
                                log.warning(
                                    "webhook action failed: url=%s error=%s", url, exc
                                )
                    elif arule["action_type"] == "send_ai_reply":
                        # action_value = system prompt / persona description
                        system_prompt = (
                            arule.get("action_value") or "Ты полезный ассистент."
                        )
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
                                headers = {
                                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                                    "Content-Type": "application/json",
                                }
                                async with http.post(
                                    "https://api.openai.com/v1/chat/completions",
                                    json=ai_payload,
                                    headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as resp:
                                    if resp.status == 200:
                                        ai_data = await resp.json()
                                        ai_text = ai_data["choices"][0]["message"][
                                            "content"
                                        ].strip()
                                        if _is_free:
                                            ai_text = brand_injection.add_promo(ai_text, html=False, context="broadcast")
                                        await bot_api.send_message(
                                            http, token, chat_id, ai_text
                                        )
                        except Exception as exc:
                            log.warning("send_ai_reply failed: %s", exc)

            # tag_added rules: fire once for each tag added above (one level, no recursion)
            for arule in automation_rules:
                if (
                    arule["trigger_type"] == "tag_added"
                    and arule.get("trigger_value")
                    and arule["trigger_value"] in newly_added_tags
                ):
                    if arule["action_type"] == "send_message":
                        _arule_text = arule["action_value"]
                        if _is_free:
                            _arule_text = brand_injection.add_promo(_arule_text, html=True, context="broadcast")
                        await bot_api.send_message(
                            http, token, chat_id, _arule_text
                        )
                    elif arule["action_type"] == "add_tag":
                        await db.add_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                    elif arule["action_type"] == "remove_tag":
                        await db.remove_user_tag(
                            pool, bot_id, chat_id, arule["action_value"]
                        )
                    elif arule["action_type"] == "subscribe_funnel":
                        try:
                            await db.subscribe_to_funnel(
                                pool, int(arule["action_value"]), chat_id
                            )
                        except (ValueError, TypeError):
                            log_exc_swallow(
                                log,
                                "Неверный funnel_id в правиле авто-ответа (блок 2)",
                                rule_id=arule.get("id"),
                                action_value=arule.get("action_value"),
                            )
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
                                async with http.post(
                                    url,
                                    json=payload,
                                    timeout=aiohttp.ClientTimeout(total=10),
                                ) as resp:
                                    log.debug(
                                        "webhook action: url=%s status=%s",
                                        url,
                                        resp.status,
                                    )
                            except Exception as exc:
                                log.warning(
                                    "webhook action failed: url=%s error=%s", url, exc
                                )
                    elif arule["action_type"] == "send_ai_reply":
                        system_prompt = (
                            arule.get("action_value") or "Ты полезный ассистент."
                        )
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
                                headers = {
                                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                                    "Content-Type": "application/json",
                                }
                                async with http.post(
                                    "https://api.openai.com/v1/chat/completions",
                                    json=ai_payload,
                                    headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30),
                                ) as resp:
                                    if resp.status == 200:
                                        ai_data = await resp.json()
                                        ai_text = ai_data["choices"][0]["message"][
                                            "content"
                                        ].strip()
                                        if _is_free:
                                            ai_text = brand_injection.add_promo(ai_text, html=False, context="broadcast")
                                        await bot_api.send_message(
                                            http, token, chat_id, ai_text
                                        )
                        except Exception as exc:
                            log.warning("send_ai_reply failed: %s", exc)

            # Funnels: subscribe on /start, keyword, or new-user join
            for funnel in funnels:
                if funnel["trigger_type"] == "start" and is_start:
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)
                elif funnel["trigger_type"] == "join" and is_new_user:
                    # Fire once for first-ever message from a new user
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)
                elif (
                    funnel["trigger_type"] == "keyword"
                    and funnel["keyword"]
                    and funnel["keyword"].lower() in text.lower()
                ):
                    await db.subscribe_to_funnel(pool, funnel["id"], chat_id)

            # A/B experiment: assign variant on /start and SEND the variant content
            if is_start and active_exp:
                variant = await db.assign_experiment_variant(
                    pool, bot_id, chat_id, active_exp["id"]
                )
                if variant and variant.get("content"):
                    exp_content = variant["content"]
                    if _is_free:
                        exp_content = brand_injection.add_promo(exp_content, html=True, context="broadcast")
                    await bot_api.send_message(http, token, chat_id, exp_content)
            elif not is_start and active_exp:
                # Conversion: any subsequent message from an assigned user counts
                try:
                    await db.record_experiment_conversion(
                        pool, bot_id, chat_id, active_exp["id"]
                    )
                except Exception:
                    log_exc_swallow(
                        log,
                        "Сбой record_experiment_conversion",
                        bot_id=bot_id,
                        chat_id=chat_id,
                    )

            # Relay: forward message to operator if relay is enabled for this bot
            if bot_row and bot_row.get("relay_enabled") and bot_row.get("added_by") and main_bot:
                try:
                    operator_id = bot_row["added_by"]
                    username = from_user.get("username")
                    first_name_r = from_user.get("first_name", "")
                    bot_label = (
                        f"@{bot_row['username']}"
                        if bot_row.get("username")
                        else (bot_row.get("first_name") or str(bot_id))
                    )
                    user_label = (
                        f"@{username}"
                        if username
                        else (f"{first_name_r} {from_user.get('last_name', '')}".strip() or f"ID:{chat_id}")
                    )
                    session_id = await db.get_or_create_relay_session(
                        pool, bot_id, chat_id, username, first_name_r
                    )
                    fwd_text = (
                        f"📨 <b>{bot_label}</b>  |  👤 {user_label}\n"
                        f"<i>ID: {chat_id}</i>\n\n"
                        f"{text}\n\n"
                        f"<i>← Reply здесь чтобы ответить пользователю</i>"
                    )
                    sent = await main_bot.send_message(
                        operator_id, fwd_text, parse_mode="HTML"
                    )
                    await db.save_relay_message(
                        pool, session_id, "in", text,
                        sent.message_id if sent else None,
                    )
                except Exception as _relay_err:
                    log.warning("auto_responder: relay forward failed bot=%d: %s", bot_id, _relay_err)

        if max_update_id > offset:
            await db.set_update_offset(pool, bot_id, max_update_id)

    except Exception:
        log.exception("Auto-responder error for bot %d", bot_id)


async def run(pool: asyncpg.Pool, http: aiohttp.ClientSession, main_bot=None) -> None:
    asyncio.get_event_loop().create_task(run_inactivity_sweep(pool, http))
    # Stagger startup — don't hammer DB immediately alongside other services
    await asyncio.sleep(10)
    while True:
        try:
            bots = await db.get_bots_for_polling(pool)
            if bots:
                await asyncio.gather(
                    *(
                        _process_bot(pool, http, b["bot_id"], b["token"], main_bot)
                        for b in bots
                    ),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Auto-responder loop error")
        await asyncio.sleep(10)


async def run_inactivity_sweep(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Background sweep: fire inactivity automation rules."""
    await asyncio.sleep(600)  # startup delay 10 min
    while True:
        try:
            await _inactivity_sweep(pool, http)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inactivity_sweep error")
        await asyncio.sleep(3600)  # hourly


async def _inactivity_sweep(pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    """Find users inactive for N days and fire matching rules."""
    # Get all active inactivity rules
    from database.db import fetch_bots as _fetch_bots_ar
    rules = await _fetch_bots_ar(
        pool,
        """SELECT ar.*, mb.token
           FROM automation_rules ar
           JOIN managed_bots mb ON mb.bot_id = ar.bot_id
           WHERE ar.trigger_type = 'inactivity'
             AND ar.is_active = true
             AND mb.is_active = true""",
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
            """SELECT ua.user_id FROM user_activity ua
               WHERE ua.bot_id = $1
                 AND ua.last_seen < NOW() - ($2 * INTERVAL '1 day')
                 AND NOT EXISTS (
                     SELECT 1 FROM inactivity_alerts_sent ias
                     WHERE ias.bot_id = $1
                       AND ias.chat_id = ua.user_id
                       AND ias.rule_id = $3
                       AND ias.sent_at > NOW() - ($2 * INTERVAL '1 day')
                 )
               LIMIT 100""",
            rule["bot_id"],
            inactivity_days,
            rule["id"],
        )

        _rule_is_free = False
        try:
            _rule_is_free = await brand_injection.is_free_tier(pool, rule["bot_id"])
        except Exception:
            pass

        for user in inactive_users:
            chat_id = user["user_id"]
            try:
                if rule["action_type"] == "send_message":
                    _inact_text = rule["action_value"]
                    if _rule_is_free:
                        _inact_text = brand_injection.add_promo(_inact_text, html=True, context="broadcast")
                    await bot_api.send_message(
                        http, rule["token"], chat_id, _inact_text
                    )
                elif rule["action_type"] == "add_tag":
                    await db.add_user_tag(
                        pool, rule["bot_id"], chat_id, rule["action_value"]
                    )
                elif rule["action_type"] == "remove_tag":
                    await db.remove_user_tag(
                        pool, rule["bot_id"], chat_id, rule["action_value"]
                    )
                elif rule["action_type"] == "create_deal":
                    title_prefix = rule.get("action_value") or "Реактивация"
                    await pool.execute(
                        """INSERT INTO crm_deals
                               (bot_id, user_id, title, status, created_at)
                           VALUES ($1, $2, $3, 'new', NOW())
                           ON CONFLICT DO NOTHING""",
                        rule["bot_id"],
                        chat_id,
                        f"{title_prefix} — id{chat_id}",
                    )
                elif rule["action_type"] == "webhook":
                    url = (rule["action_value"] or "").strip()
                    if url:
                        await http.post(
                            url,
                            json={
                                "bot_id": rule["bot_id"],
                                "chat_id": chat_id,
                                "trigger_type": "inactivity",
                                "inactivity_days": inactivity_days,
                            },
                            timeout=aiohttp.ClientTimeout(total=10),
                        )

                # Mark as sent (prevent duplicate)
                await pool.execute(
                    """INSERT INTO inactivity_alerts_sent(bot_id, chat_id, rule_id)
                       VALUES($1, $2, $3)
                       ON CONFLICT(bot_id, chat_id, rule_id) DO UPDATE SET sent_at=NOW()""",
                    rule["bot_id"],
                    chat_id,
                    rule["id"],
                )
                await asyncio.sleep(0.1)  # tiny delay between messages
            except Exception as exc:
                log.warning("inactivity_sweep: failed for chat=%s: %s", chat_id, exc)

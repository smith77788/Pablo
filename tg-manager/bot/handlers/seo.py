"""SEO score analyzer and optimizer for bots, channels and groups."""
from __future__ import annotations
import asyncio
import html
import json
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg
from bot.callbacks import SeoCb, EditCb, ChanFactCb
from bot.keyboards import seo_menu, subscription_locked_markup
from bot.utils.subscription import require_plan, locked_text
from database import db
from services import bot_api

log = logging.getLogger(__name__)
router = Router()


def _seo_score(name: str, description: str, short_desc: str,
               commands: list, audience: int) -> tuple[int, list[str]]:
    score = 0
    tips = []

    # Name: 0–20 pts
    name_len = len(name) if name else 0
    if name_len >= 3:   score += 8
    if name_len >= 8:   score += 7
    if name_len <= 30:  score += 5
    if not name or name_len < 3:
        tips.append("📛 Имя слишком короткое — добавьте ключевые слова в название")
    elif name_len > 32:
        tips.append("📛 Имя длиннее 32 симв. — Telegram обрежет его в результатах поиска")

    # Description: 0–30 pts
    desc_len = len(description) if description else 0
    if desc_len >= 50:   score += 10
    if desc_len >= 200:  score += 10
    if desc_len >= 400:  score += 10
    if not description:
        tips.append("📄 Описание пустое — это критично для SEO! Добавьте его в «✏️ Профиль»")
    elif desc_len < 150:
        tips.append(f"📄 Описание короткое ({desc_len}/512 симв.) — добавьте функции и ключевые слова")
    elif desc_len < 350:
        tips.append(f"📄 Описание можно расширить ({desc_len}/512 симв.) — используйте всё пространство")

    # Short description: 0–15 pts
    short_len = len(short_desc) if short_desc else 0
    if short_len >= 10:  score += 7
    if short_len >= 50:  score += 8
    if not short_desc:
        tips.append("📃 Краткое описание пустое — оно показывается в поиске! Добавьте CTA")
    elif short_len < 50:
        tips.append(f"📃 Краткое описание мало ({short_len}/120 симв.) — добавьте призыв к действию")

    # Commands: 0–15 pts
    cmd_count = len(commands)
    if cmd_count >= 1:   score += 5
    if cmd_count >= 3:   score += 5
    if cmd_count >= 6:   score += 5
    if cmd_count == 0:
        tips.append("🤖 Нет команд — добавьте /help, /start и др. через раздел «🤖 Команды»")
    elif cmd_count < 3:
        tips.append(f"🤖 Мало команд ({cmd_count}) — добавьте ещё, это повышает доверие к боту")

    # Audience: 0–20 pts
    if audience >= 10:     score += 5
    if audience >= 100:    score += 5
    if audience >= 1000:   score += 5
    if audience >= 10000:  score += 5
    if audience < 100:
        tips.append(f"👥 Аудитория мала ({audience} чел.) — растите базу через «🔗 Диплинки»")
    elif audience < 1000:
        tips.append(f"👥 Хороший старт! Цель: 1 000+ пользователей (сейчас {audience})")

    return min(score, 100), tips


def _score_bar(score: int) -> str:
    filled = round(score / 10)
    empty = 10 - filled
    if score >= 75:
        color = "🟩"
    elif score >= 45:
        color = "🟨"
    else:
        color = "🟥"
    return f"{color * filled}{'⬜' * empty} {score}/100"


@router.callback_query(SeoCb.filter(F.action == "menu"))
async def cb_seo_menu(callback: CallbackQuery, callback_data: SeoCb,
                       pool: asyncpg.Pool) -> None:

    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO и аналитика поиска", "starter"), parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"📈 <b>SEO — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "SEO помогает вашему боту занимать более высокие места в поиске Telegram. Чем выше бот в поиске — тем больше людей его найдут и подпишутся.\n\n"
        "💡 <b>Что вы можете здесь делать:</b>\n"
        "• <b>Анализ профиля</b> — получите оценку от 0 до 100 и список конкретных улучшений\n"
        "• <b>Ключевые слова</b> — посмотрите, что пишут ваши пользователи, и добавьте эти слова в описание\n"
        "• <b>SEO-советы</b> — пошаговые инструкции по оптимизации",
        parse_mode="HTML",
        reply_markup=seo_menu(callback_data.bot_id),
    )


@router.callback_query(SeoCb.filter(F.action == "analyze"))
async def cb_seo_analyze(callback: CallbackQuery, callback_data: SeoCb,
                          pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.answer("⏳ Анализирую профиль...")

    token = row["token"]
    name, description, short_desc, commands = await asyncio.gather(
        bot_api.get_my_name(http, token),
        bot_api.get_my_description(http, token),
        bot_api.get_my_short_description(http, token),
        bot_api.get_my_commands(http, token),
        return_exceptions=True,
    )
    if isinstance(name, Exception):        name = row.get("first_name", "")
    if isinstance(description, Exception): description = ""
    if isinstance(short_desc, Exception):  short_desc = ""
    if isinstance(commands, Exception):    commands = []

    audience = await pool.fetchval(
        "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", callback_data.bot_id
    ) or 0

    score, tips = _seo_score(name, description, short_desc, commands or [], int(audience))
    bar = _score_bar(score)

    label = f"@{row['username']}" if row["username"] else row["first_name"]
    lines = [
        f"📈 <b>SEO-скор профиля — {label}</b>\n",
        f"<b>{bar}</b>\n",
        "<b>Параметры профиля:</b>",
        f"  📛 Имя: {len(name or '')} симв.",
        f"  📄 Описание: {len(description or '')} / 512 симв.",
        f"  📃 Краткое: {len(short_desc or '')} / 120 симв.",
        f"  🤖 Команд: {len(commands or [])}",
        f"  👥 Аудитория: {int(audience):,} чел.",
    ]
    if tips:
        lines.append("\n<b>Что улучшить:</b>")
        for t in tips:
            lines.append(f"  • {t}")
    if score >= 80:
        lines.append("\n✅ <b>Отлично!</b> Профиль хорошо оптимизирован.")
    elif score >= 55:
        lines.append("\n🟡 <b>Хороший старт.</b> Следуйте советам выше.")
    else:
        lines.append("\n🔴 <b>Требует доработки.</b> Начните с описания бота.")

    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать профиль",
              callback_data=EditCb(action="menu", bot_id=callback_data.bot_id))
    kb.button(text="🔄 Обновить анализ",
              callback_data=SeoCb(action="analyze", bot_id=callback_data.bot_id))
    kb.button(text="◀️ Назад к SEO",
              callback_data=SeoCb(action="menu", bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(SeoCb.filter(F.action == "keywords"))
async def cb_seo_keywords(callback: CallbackQuery, callback_data: SeoCb,
                           pool: asyncpg.Pool) -> None:

    await callback.answer()
    keywords = await db.get_top_keywords(pool, callback_data.bot_id, limit=20)
    summary = await db.get_keyword_stats_summary(pool, callback_data.bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=callback_data.bot_id))

    if keywords:
        max_cnt = keywords[0]["count"]
        lines = []
        for kw in keywords:
            bar_len = round(kw["count"] / max_cnt * 8) if max_cnt else 0
            bar = "▓" * bar_len + "░" * (8 - bar_len)
            lines.append(f"<code>{kw['keyword']:<14}</code>{bar} {kw['count']}")
        body = "\n".join(lines)
        hint = "\n\n<i>💡 Добавьте популярные слова в описание бота — это улучшит SEO</i>"
    else:
        body = "Нет данных. Ключевые слова накапливаются по мере сообщений пользователей."
        hint = ""

    await callback.message.edit_text(
        f"🔑 <b>Ключевые слова пользователей</b>\n\n"
        f"Уникальных слов: <b>{summary['total_keywords']}</b>\n"
        f"Сообщений обработано: <b>{summary['total_messages']}</b>\n\n"
        f"<b>Топ-20:</b>\n{body}{hint}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SeoCb.filter(F.action == "tips"))
async def cb_seo_tips(callback: CallbackQuery, callback_data: SeoCb) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Запустить анализ",
              callback_data=SeoCb(action="analyze", bot_id=callback_data.bot_id))
    kb.button(text="◀️ Назад",
              callback_data=SeoCb(action="menu", bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        "💡 <b>SEO-советы для роста в поиске Telegram</b>\n\n"
        "<b>1. Имя бота — самый важный фактор</b>\n"
        "Telegram ранжирует ботов по соответствию запросу. Включите ключевое слово прямо в имя.\n"
        "✅ Хорошо: «PDF Converter Bot», «Crypto Alerts RU»\n"
        "❌ Плохо: «MyBot», «Bot123»\n\n"
        "<b>2. Описание — заполняйте все 512 символов</b>\n"
        "Используйте синонимы, разные формы слов, конкретные функции бота. "
        "Telegram индексирует описание для поиска.\n\n"
        "<b>3. Краткое описание — ваш заголовок в поиске</b>\n"
        "Это первое что видит пользователь. Сделайте его конкретным: что делает бот + CTA.\n\n"
        "<b>4. Команды = профессионализм</b>\n"
        "Боты без команд выглядят незавершёнными. Минимум: /start, /help.\n\n"
        "<b>5. Аудитория — самый весомый сигнал</b>\n"
        "Telegram продвигает популярных ботов. Растите базу через:\n"
        "  • Диплинки с UTM-трекингом\n"
        "  • Реферальную систему (реф. ссылки для юзеров)\n"
        "  • Кросс-промо в каналах\n\n"
        "<b>6. Удержание важнее привлечения</b>\n"
        "Реактивируйте холодных юзеров — engagement rate влияет на ранжирование.\n\n"
        "<b>7. A/B тесты /start сообщения</b>\n"
        "Разные приветствия дают разный retention. Используйте раздел «🧪 A/B Тесты».\n\n"
        "<b>8. Регулярность = свежесть</b>\n"
        "Боты с активными рассылками получают приоритет. Пишите хотя бы раз в неделю.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


# ══════════════════════════════════════════════════════════════════
# SEO OPTIMIZER — CHANNELS & GROUPS
# ══════════════════════════════════════════════════════════════════

def _chan_seo_score(title: str, about: str, username: str, members: int) -> tuple[int, list[str]]:
    """Calculate SEO score 0-100 for a channel or group."""
    score = 0
    tips: list[str] = []

    # Title: 0-25 pts
    tlen = len(title) if title else 0
    if tlen >= 5:    score += 8
    if tlen >= 15:   score += 10
    if tlen <= 35:   score += 7
    if tlen < 5:
        tips.append("📛 Название слишком короткое — добавьте ключевые слова")
    elif tlen > 50:
        tips.append("📛 Название слишком длинное (>50 симв.) — Telegram обрежет в поиске")
    elif tlen < 15:
        tips.append(f"📛 Название можно расширить ({tlen}/50 симв.) — добавьте тематику")

    # About/description: 0-35 pts
    alen = len(about) if about else 0
    if alen >= 30:    score += 10
    if alen >= 150:   score += 12
    if alen >= 300:   score += 13
    if not about:
        tips.append("📄 Описание пустое — это критично! Добавьте описание с ключевыми словами")
    elif alen < 100:
        tips.append(f"📄 Описание короткое ({alen}/255 симв.) — расширьте до 150+ символов")
    elif alen < 200:
        tips.append(f"📄 Описание можно улучшить ({alen}/255 симв.) — используйте всё пространство")

    # Username: 0-20 pts
    if username:
        ulen = len(username)
        score += 10
        if 5 <= ulen <= 20:  score += 5
        if not any(c.isdigit() for c in username):  score += 5
        if ulen > 25:
            tips.append(f"🔗 Username длинный ({ulen} симв.) — короткий username запоминается лучше")
    else:
        tips.append("🔗 Нет username — без него канал значительно хуже ранжируется в поиске")

    # Members: 0-20 pts
    if members >= 100:    score += 5
    if members >= 1_000:  score += 5
    if members >= 10_000: score += 5
    if members >= 50_000: score += 5
    if members < 100:
        tips.append(f"👥 Мало подписчиков ({members}) — используйте массовые инвайты и кросс-промо")
    elif members < 1_000:
        tips.append(f"👥 Растущий канал! Цель — 1 000+ подписчиков (сейчас {members:,})")

    return min(score, 100), tips


async def _ai_generate_seo(
    http: aiohttp.ClientSession,
    title: str, about: str, username: str,
    entity_type: str, keywords: list[str],
) -> dict | None:
    """Generate SEO-optimized title, description, username via OpenRouter."""
    try:
        from config import OPENROUTER_API_KEY, OPENROUTER_MODEL
        if not OPENROUTER_API_KEY:
            return None
    except ImportError:
        return None

    kw_hint = ", ".join(keywords[:10]) if keywords else "—"
    # If channel already has a username, instruct AI to keep it unchanged and use it in about
    if username:
        username_rule = f'- username: MUST return exactly "{username}" — do NOT change it'
        about_rule = f'- about: 150-255 chars, include 3-5 keywords naturally, mention @{username} once naturally (e.g. in CTA), ends with CTA'
    else:
        username_rule = "- username: 5-20 chars, lowercase, letters/digits/underscores only, no leading digits"
        about_rule = "- about: 150-255 chars, include 3-5 keywords naturally, ends with CTA"
    prompt = (
        f"You are a Telegram SEO expert. Optimize the following {entity_type} profile for maximum search visibility.\n\n"
        f"Current title: {title or '(empty)'}\n"
        f"Current description: {about[:200] if about else '(empty)'}\n"
        f"Current username: @{username if username else '(none)'}\n"
        f"Top user keywords: {kw_hint}\n\n"
        "Return ONLY valid JSON with these keys:\n"
        '{"title": "...", "about": "...", "username": "...", "reasoning": "..."}\n\n'
        "Rules:\n"
        "- title: max 50 chars, include main keyword, catchy\n"
        f"- {about_rule}\n"
        f"- {username_rule}\n"
        "- reasoning: 1-2 sentences explaining the strategy\n"
        "Write in the same language as the current profile (or Russian if empty)."
    )

    try:
        async with http.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 400,
                "temperature": 0.7,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            raw = data["choices"][0]["message"]["content"].strip()
            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
    except Exception as e:
        log.warning("AI SEO generate error: %s", e)
        return None


# ── Channel/Group SEO menu ────────────────────────────────────────

@router.callback_query(SeoCb.filter(F.action == "chan_menu"))
async def cb_seo_chan_menu(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO-оптимизация канала", "starter"), parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter"),
        )
        return
    await callback.answer()
    chan_id = callback_data.chan_id
    acc_id  = callback_data.acc_id

    chan = await pool.fetchrow(
        "SELECT title, username, access_hash FROM managed_channels WHERE id=$1 AND owner_id=$2",
        chan_id, callback.from_user.id,
    )
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return

    name = f"@{chan['username']}" if chan.get("username") else html.escape(chan["title"] or "")

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Анализ SEO-скора",   callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id))
    kb.button(text="🤖 AI-оптимизация",     callback_data=SeoCb(action="chan_ai",      chan_id=chan_id, acc_id=acc_id))
    kb.button(text="✏️ Применить изменения", callback_data=SeoCb(action="chan_apply",   chan_id=chan_id, acc_id=acc_id))
    kb.button(text="💡 SEO-советы",          callback_data=SeoCb(action="tips", bot_id=0))
    kb.button(text="◀️ Назад",               callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📈 <b>SEO-оптимизация — {name}</b>\n\n"
        "Telegram-поиск ранжирует каналы по названию, описанию и username.\n"
        "Правильно оптимизированный канал находят в 3-5 раз чаще.\n\n"
        "• <b>Анализ</b> — текущий SEO-скор с конкретными рекомендациями\n"
        "• <b>AI-оптимизация</b> — ИИ напишет title/about/username под ваш контент\n"
        "• <b>Применить</b> — обновить поля канала в один клик",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Channel SEO analysis ──────────────────────────────────────────

@router.callback_query(SeoCb.filter(F.action == "chan_analyze"))
async def cb_seo_chan_analyze(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Анализирую...")
    chan_id = callback_data.chan_id
    acc_id  = callback_data.acc_id
    user_id = callback.from_user.id

    chan = await pool.fetchrow(
        "SELECT id, title, username, access_hash, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
        chan_id, user_id,
    )
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return

    # Try to get full about from Telethon
    about = ""
    members = 0
    acc = await pool.fetchrow(
        "SELECT session_str, device_model, system_version, app_version "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id, user_id,
    ) if acc_id else None

    if acc:
        try:
            from services import account_manager
            info = await account_manager.get_full_channel_info(
                acc["session_str"], chan["channel_id"], _acc=acc
            )
            if info:
                about = info.get("about", "")
                members = info.get("members_count", 0) or 0
        except Exception as e:
            log.debug("chan seo get_full_channel_info: %s", e)

    title = chan["title"] or ""
    username = chan["username"] or ""
    score, tips = _chan_seo_score(title, about, username, members)
    bar = _score_bar(score)

    display = f"@{username}" if username else html.escape(title)
    lines = [
        f"📊 <b>SEO-скор — {display}</b>\n",
        f"<b>{bar}</b>\n",
        "<b>Параметры:</b>",
        f"  📛 Название: <b>{html.escape(title)}</b>  ({len(title)} симв.)",
        f"  📄 Описание: {len(about)} симв.",
        f"  🔗 Username: {'@' + html.escape(username) if username else '⚠️ нет'}",
        f"  👥 Подписчиков: {members:,}",
    ]
    if tips:
        lines.append("\n<b>Что улучшить:</b>")
        for t in tips:
            lines.append(f"  • {t}")
    if score >= 80:
        lines.append("\n✅ <b>Отличный SEO!</b> Канал хорошо виден в поиске.")
    elif score >= 50:
        lines.append("\n🟡 <b>Средний SEO.</b> Следуйте советам выше.")
    else:
        lines.append("\n🔴 <b>Слабый SEO.</b> Начните с добавления описания и username.")

    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 AI-оптимизация",  callback_data=SeoCb(action="chan_ai",    chan_id=chan_id, acc_id=acc_id))
    kb.button(text="🔄 Обновить анализ", callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id))
    kb.button(text="◀️ Назад",           callback_data=SeoCb(action="chan_menu",  chan_id=chan_id, acc_id=acc_id))
    kb.adjust(1)
    try:
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


# ── AI SEO generation ─────────────────────────────────────────────

@router.callback_query(SeoCb.filter(F.action == "chan_ai"))
async def cb_seo_chan_ai(
    callback: CallbackQuery, callback_data: SeoCb,
    pool: asyncpg.Pool, http: aiohttp.ClientSession,
) -> None:
    await callback.answer("🤖 Генерирую SEO-текст...")
    chan_id = callback_data.chan_id
    acc_id  = callback_data.acc_id
    user_id = callback.from_user.id

    chan = await pool.fetchrow(
        "SELECT title, username, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
        chan_id, user_id,
    )
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return

    # Get current about
    about = ""
    acc = await pool.fetchrow(
        "SELECT session_str, device_model, system_version, app_version "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id, user_id,
    ) if acc_id else None

    if acc:
        try:
            from services import account_manager
            info = await account_manager.get_full_channel_info(
                acc["session_str"], chan["channel_id"], _acc=acc
            )
            if info:
                about = info.get("about", "")
        except Exception:
            pass

    # Get top keywords from tracked_keywords + search_memory
    kw_rows = await pool.fetch(
        """SELECT keyword FROM search_memory WHERE owner_id=$1
           ORDER BY search_count DESC LIMIT 10""",
        user_id,
    )
    keywords = [r["keyword"] for r in kw_rows]

    result = await _ai_generate_seo(
        http,
        title=chan["title"] or "",
        about=about,
        username=chan["username"] or "",
        entity_type="Telegram channel",
        keywords=keywords,
    )

    if not result:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id))
        await callback.message.edit_text(
            "⚠️ <b>AI-генерация недоступна</b>\n\n"
            "Для работы AI-оптимизации нужен OPENROUTER_API_KEY в настройках Railway.\n\n"
            "Вы можете применить изменения вручную через кнопку «✏️ Применить».",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    new_title    = result.get("title", "")
    new_about    = result.get("about", "")
    new_username = result.get("username", "")
    reasoning    = result.get("reasoning", "")

    # Save to FSM-like temp storage in pool (simple approach: store in state via message edit + buttons)
    lines = [
        "🤖 <b>AI-предложение по SEO</b>\n",
        f"📛 <b>Название:</b> {html.escape(new_title)}",
        f"📄 <b>Описание:</b>\n<i>{html.escape(new_about)}</i>",
        f"🔗 <b>Username:</b> @{html.escape(new_username)}",
    ]
    if reasoning:
        lines.append(f"\n💡 <i>{html.escape(reasoning)}</i>")

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Применить всё",     callback_data=SeoCb(action="apply_all",   chan_id=chan_id, acc_id=acc_id))
    kb.button(text="📛 Только название",   callback_data=SeoCb(action="apply_title", chan_id=chan_id, acc_id=acc_id))
    kb.button(text="📄 Только описание",   callback_data=SeoCb(action="apply_about", chan_id=chan_id, acc_id=acc_id))
    kb.button(text="🔗 Только username",   callback_data=SeoCb(action="apply_uname", chan_id=chan_id, acc_id=acc_id))
    kb.button(text="🔄 Перегенерировать",  callback_data=SeoCb(action="chan_ai",     chan_id=chan_id, acc_id=acc_id))
    kb.button(text="◀️ Назад",             callback_data=SeoCb(action="chan_menu",   chan_id=chan_id, acc_id=acc_id))
    kb.adjust(1, 3, 2)

    # Store suggestions in DB via pool temp table or encode in message — use temp pool execute
    try:
        await pool.execute(
            """INSERT INTO seo_ai_suggestions(owner_id, chan_id, title, about, username, created_at)
               VALUES($1,$2,$3,$4,$5,now())
               ON CONFLICT(owner_id, chan_id) DO UPDATE
               SET title=$3, about=$4, username=$5, created_at=now()""",
            user_id, chan_id, new_title, new_about, new_username,
        )
    except Exception:
        # Table may not exist yet — we'll handle inline via the message
        pass

    try:
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


# ── Apply optimizations ───────────────────────────────────────────

async def _get_seo_suggestion(pool: asyncpg.Pool, user_id: int, chan_id: int) -> dict:
    """Retrieve last AI suggestion for a channel."""
    try:
        row = await pool.fetchrow(
            "SELECT title, about, username FROM seo_ai_suggestions WHERE owner_id=$1 AND chan_id=$2",
            user_id, chan_id,
        )
        return dict(row) if row else {}
    except Exception:
        return {}


async def _apply_chan_field(
    pool: asyncpg.Pool, user_id: int, chan_id: int, acc_id: int,
    field: str, value: str,
) -> tuple[bool, str]:
    """Apply a single field change to channel via Telethon."""
    chan = await pool.fetchrow(
        "SELECT channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
        chan_id, user_id,
    )
    acc = await pool.fetchrow(
        "SELECT session_str, device_model, system_version, app_version "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id, user_id,
    ) if acc_id else None

    if not chan or not acc:
        return False, "Канал или аккаунт не найден"

    from services import account_manager
    tg_chan_id = chan["channel_id"]

    if field == "title":
        ok = await account_manager.edit_channel_title(acc["session_str"], tg_chan_id, value, _acc=acc)
        if ok:
            await pool.execute(
                "UPDATE managed_channels SET title=$1 WHERE id=$2", value, chan_id
            )
        return ok, "" if ok else "Ошибка обновления названия"
    elif field == "about":
        ok = await account_manager.edit_channel_about(acc["session_str"], tg_chan_id, value, _acc=acc)
        return ok, "" if ok else "Ошибка обновления описания"
    elif field == "username":
        err = await account_manager.set_channel_username(acc["session_str"], tg_chan_id, value, _acc=acc)
        if not err:
            await pool.execute(
                "UPDATE managed_channels SET username=$1 WHERE id=$2", value, chan_id
            )
        return not bool(err), err
    return False, "Неизвестное поле"


@router.callback_query(SeoCb.filter(F.action.in_({"apply_all", "apply_title", "apply_about", "apply_uname", "chan_apply"})))
async def cb_seo_apply(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Применяю...")
    chan_id = callback_data.chan_id
    acc_id  = callback_data.acc_id
    user_id = callback.from_user.id
    action  = callback_data.action

    if action == "chan_apply":
        # Show manual edit prompt: pick what to change
        kb = InlineKeyboardBuilder()
        kb.button(text="📛 Изменить название",  callback_data=SeoCb(action="edit_title", chan_id=chan_id, acc_id=acc_id))
        kb.button(text="📄 Изменить описание",  callback_data=SeoCb(action="edit_about", chan_id=chan_id, acc_id=acc_id))
        kb.button(text="🔗 Изменить username",  callback_data=SeoCb(action="edit_uname", chan_id=chan_id, acc_id=acc_id))
        kb.button(text="◀️ Назад",              callback_data=SeoCb(action="chan_menu",  chan_id=chan_id, acc_id=acc_id))
        kb.adjust(1)
        await callback.message.edit_text(
            "✏️ <b>Применить SEO-изменения</b>\n\n"
            "Выберите что изменить прямо сейчас:",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
        return

    suggestion = await _get_seo_suggestion(pool, user_id, chan_id)
    if not suggestion:
        await callback.answer("Сначала запустите AI-оптимизацию.", show_alert=True)
        return

    results = []
    if action in ("apply_all", "apply_title") and suggestion.get("title"):
        ok, err = await _apply_chan_field(pool, user_id, chan_id, acc_id, "title", suggestion["title"])
        results.append(f"📛 Название: {'✅' if ok else '❌ ' + err}")

    if action in ("apply_all", "apply_about") and suggestion.get("about"):
        ok, err = await _apply_chan_field(pool, user_id, chan_id, acc_id, "about", suggestion["about"])
        results.append(f"📄 Описание: {'✅' if ok else '❌ ' + err}")

    if action in ("apply_all", "apply_uname") and suggestion.get("username"):
        ok, err = await _apply_chan_field(pool, user_id, chan_id, acc_id, "username", suggestion["username"])
        results.append(f"🔗 Username: {'✅' if ok else '❌ ' + err}")

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Новый анализ", callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id))
    kb.button(text="◀️ Назад",        callback_data=SeoCb(action="chan_menu",   chan_id=chan_id, acc_id=acc_id))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            "<b>✏️ Результат применения SEO</b>\n\n" + "\n".join(results or ["Нечего применять."]),
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise

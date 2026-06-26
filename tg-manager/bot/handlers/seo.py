"""SEO score analyzer and optimizer for bots, channels and groups."""

from __future__ import annotations
import asyncio
import html
import json
import logging
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg
from bot.callbacks import SeoCb, EditCb, ChanFactCb, BmCb
from bot.keyboards import subscription_locked_markup
from bot.utils.subscription import require_plan, locked_text
from bot.states import SeoFSM
from database import db
from services import bot_api
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()


def _seo_score(
    name: str,
    description: str,
    short_desc: str,
    commands: list,
    audience: int,
    username: str = "",
) -> tuple[int, list[str]]:
    score = 0
    tips = []

    # Username: 0–15 pts (key search signal for bots)
    ulen = len(username) if username else 0
    if username:
        score += 8
        if 5 <= ulen <= 20:
            score += 4
        if not any(c.isdigit() for c in username):
            score += 3
        if ulen > 25:
            tips.append(
                f"🔗 Username длинный ({ulen} симв.) — короткий запоминается лучше"
            )
    else:
        tips.append(
            "🔗 Username не задан — он определяет вашу позицию в поиске Telegram по @имени"
        )

    # Name: 0–20 pts
    name_len = len(name) if name else 0
    if name_len >= 3:
        score += 8
    if name_len >= 8:
        score += 7
    if name_len <= 30:
        score += 5
    if not name or name_len < 3:
        tips.append("📛 Имя слишком короткое — добавьте ключевые слова в название")
    elif name_len > 32:
        tips.append(
            "📛 Имя длиннее 32 симв. — Telegram обрежет его в результатах поиска"
        )

    # Description: 0–30 pts
    desc_len = len(description) if description else 0
    if desc_len >= 50:
        score += 10
    if desc_len >= 200:
        score += 10
    if desc_len >= 400:
        score += 10
    if not description:
        tips.append(
            "📄 Описание пустое — это критично для SEO! Добавьте его в «✏️ Профиль»"
        )
    elif desc_len < 150:
        tips.append(
            f"📄 Описание короткое ({desc_len}/512 симв.) — добавьте функции и ключевые слова"
        )
    elif desc_len < 350:
        tips.append(
            f"📄 Описание можно расширить ({desc_len}/512 симв.) — используйте всё пространство"
        )

    # Short description: 0–15 pts
    short_len = len(short_desc) if short_desc else 0
    if short_len >= 10:
        score += 7
    if short_len >= 50:
        score += 8
    if not short_desc:
        tips.append(
            "📃 Краткое описание пустое — оно показывается в поиске! Добавьте CTA"
        )
    elif short_len < 50:
        tips.append(
            f"📃 Краткое описание мало ({short_len}/120 симв.) — добавьте призыв к действию"
        )

    # Commands: 0–15 pts
    cmd_count = len(commands)
    if cmd_count >= 1:
        score += 5
    if cmd_count >= 3:
        score += 5
    if cmd_count >= 6:
        score += 5
    if cmd_count == 0:
        tips.append(
            "🤖 Нет команд — добавьте /help, /start и др. через раздел «🤖 Команды»"
        )
    elif cmd_count < 3:
        tips.append(
            f"🤖 Мало команд ({cmd_count}) — добавьте ещё, это повышает доверие к боту"
        )

    # Audience: 0–20 pts
    if audience >= 10:
        score += 5
    if audience >= 100:
        score += 5
    if audience >= 1000:
        score += 5
    if audience >= 10000:
        score += 5
    if audience < 100:
        tips.append(
            f"👥 Аудитория мала ({audience} чел.) — растите базу через «🔗 Диплинки»"
        )
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
async def cb_seo_menu(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:

    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO и аналитика поиска", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
        )
        return
    try:
        row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    except Exception:
        await callback.answer("Ошибка базы данных.", show_alert=True)
        return
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    bot_uname = row.get("username") or ""
    label = f"@{bot_uname}" if bot_uname else (row.get("first_name") or "бот")
    uname_status = (
        f"🔗 Username: <b>@{html.escape(bot_uname)}</b> — ключевой сигнал для поиска"
        if bot_uname
        else "⚠️ <b>Username не задан</b> — задайте через @BotFather → /setusername"
    )
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Анализ профиля",
        callback_data=SeoCb(action="analyze", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="🔍 Превью в поиске",
        callback_data=SeoCb(action="preview", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="📈 Динамика позиций",
        callback_data=SeoCb(action="momentum", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="📊 Keyword Gap",
        callback_data=SeoCb(action="content_gap", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="🔗 Альтернативы username",
        callback_data=SeoCb(action="uname_alts", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="🔑 Ключевые слова",
        callback_data=SeoCb(action="keywords", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="📖 Гайд SEO 2026",
        callback_data=SeoCb(action="full_guide", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="✏️ Редактировать профиль",
        callback_data=EditCb(action="menu", bot_id=callback_data.bot_id),
    )
    kb.adjust(1, 2, 2, 2, 1)
    await callback.message.edit_text(
        f"📈 <b>SEO — {label}</b>\n\n"
        f"{uname_status}\n\n"
        "<b>Как Telegram находит ботов (приоритет):</b>\n"
        "1️⃣ <b>@username</b> — прямое совпадение = топ-позиция\n"
        "2️⃣ <b>Имя бота</b> — ключевые слова в названии\n"
        "3️⃣ <b>Описание</b> — полнотекстовый индекс\n"
        "4️⃣ <b>Краткое описание</b> — snippet в поиске\n\n"
        "• <b>Анализ</b> — скор 0–100 + советы\n"
        "• <b>Превью</b> — как вы выглядите в поиске\n"
        "• <b>Динамика</b> — тренд позиций по keywords\n"
        "• <b>Keyword Gap</b> — какие слова не в описании\n"
        "• <b>Гайд 2026</b> — все реальные факторы ранжирования",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SeoCb.filter(F.action == "analyze"))
async def cb_seo_analyze(
    callback: CallbackQuery,
    callback_data: SeoCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO-анализ", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
        )
        return

    try:
        row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    except Exception:
        await callback.answer("Ошибка базы данных.", show_alert=True)
        return
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer("⏳ Анализирую профиль...")

    token = row["token"]
    name, description, short_desc, commands = await asyncio.gather(
        bot_api.get_my_name(http, token),
        bot_api.get_my_description(http, token),
        bot_api.get_my_short_description(http, token),
        bot_api.get_my_commands(http, token),
        return_exceptions=True,
    )
    if isinstance(name, Exception):
        name = row.get("first_name", "")
    if isinstance(description, Exception):
        description = ""
    if isinstance(short_desc, Exception):
        short_desc = ""
    if isinstance(commands, Exception):
        commands = []

    try:
        audience = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", callback_data.bot_id
            )
            or 0
        )
    except Exception:
        log.warning("seo analyze: failed to fetch audience count")
        audience = 0

    bot_username = row.get("username") or ""
    score, tips = _seo_score(
        name,
        description,
        short_desc,
        commands or [],
        int(audience),
        username=bot_username,
    )
    bar = _score_bar(score)

    # Save SEO score to history DB
    await db.save_seo_score(
        pool,
        owner_id=callback.from_user.id,
        entity_type="bot",
        entity_id=callback_data.bot_id,
        score=score,
        tips=tips,
    )

    label = (
        f"@{bot_username}"
        if bot_username
        else (row.get("first_name") or str(row["bot_id"]))
    )
    uname_line = (
        f"  🔗 Username: <b>@{html.escape(bot_username)}</b> ✅ — индексируется в поиске"
        if bot_username
        else "  🔗 Username: <b>⚠️ не задан</b> — задайте через @BotFather → /setusername"
    )
    lines = [
        f"📈 <b>SEO-скор профиля — {label}</b>\n",
        f"<b>{bar}</b>\n",
        "<b>Параметры профиля:</b>",
        uname_line,
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
    kb.button(
        text="✏️ Редактировать профиль",
        callback_data=EditCb(action="menu", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="📜 История проверок",
        callback_data=SeoCb(action="bot_history", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="🔄 Обновить анализ",
        callback_data=SeoCb(action="analyze", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="◀️ Назад к SEO",
        callback_data=SeoCb(action="menu", bot_id=callback_data.bot_id),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SeoCb.filter(F.action == "keywords"))
async def cb_seo_keywords(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO — ключевые слова", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
        )
        return

    await callback.answer()
    keywords = await db.get_top_keywords(pool, callback_data.bot_id, limit=20)
    summary = await db.get_keyword_stats_summary(pool, callback_data.bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=callback_data.bot_id)
    )

    if keywords:
        max_cnt = keywords[0]["count"]
        lines = []
        for kw in keywords:
            bar_len = round(kw["count"] / max_cnt * 8) if max_cnt else 0
            bar = "▓" * bar_len + "░" * (8 - bar_len)
            lines.append(f"<code>{kw['keyword']:<14}</code>{bar} {kw['count']}")
        body = "\n".join(lines)
        hint = (
            "\n\n<i>💡 Добавьте популярные слова в описание бота — это улучшит SEO</i>"
        )
    else:
        body = (
            "Нет данных. Ключевые слова накапливаются по мере сообщений пользователей."
        )
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
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Запустить анализ",
        callback_data=SeoCb(action="analyze", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=callback_data.bot_id)
    )
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


# ══════════════════════════════════════════════════════════════════
# SEO OPTIMIZER — CHANNELS & GROUPS
# ══════════════════════════════════════════════════════════════════


def _chan_seo_score(
    title: str, about: str, username: str, members: int
) -> tuple[int, list[str]]:
    """Calculate SEO score 0-100 for a channel or group."""
    score = 0
    tips: list[str] = []

    # Title: 0-25 pts
    tlen = len(title) if title else 0
    if tlen >= 5:
        score += 8
    if tlen >= 15:
        score += 10
    if tlen <= 35:
        score += 7
    if tlen < 5:
        tips.append("📛 Название слишком короткое — добавьте ключевые слова")
    elif tlen > 50:
        tips.append(
            "📛 Название слишком длинное (>50 симв.) — Telegram обрежет в поиске"
        )
    elif tlen < 15:
        tips.append(
            f"📛 Название можно расширить ({tlen}/50 симв.) — добавьте тематику"
        )

    # About/description: 0-35 pts
    alen = len(about) if about else 0
    if alen >= 30:
        score += 10
    if alen >= 150:
        score += 12
    if alen >= 300:
        score += 13
    if not about:
        tips.append(
            "📄 Описание пустое — это критично! Добавьте описание с ключевыми словами"
        )
    elif alen < 100:
        tips.append(
            f"📄 Описание короткое ({alen}/255 симв.) — расширьте до 150+ символов"
        )
    elif alen < 200:
        tips.append(
            f"📄 Описание можно улучшить ({alen}/255 симв.) — используйте всё пространство"
        )

    # Username: 0-20 pts
    if username:
        ulen = len(username)
        score += 10
        if 5 <= ulen <= 20:
            score += 5
        if not any(c.isdigit() for c in username):
            score += 5
        if ulen > 25:
            tips.append(
                f"🔗 Username длинный ({ulen} симв.) — короткий username запоминается лучше"
            )
    else:
        tips.append(
            "🔗 Нет username — без него канал значительно хуже ранжируется в поиске"
        )

    # Members: 0-20 pts
    if members >= 100:
        score += 5
    if members >= 1_000:
        score += 5
    if members >= 10_000:
        score += 5
    if members >= 50_000:
        score += 5
    if members < 100:
        tips.append(
            f"👥 Мало подписчиков ({members}) — используйте массовые инвайты и кросс-промо"
        )
    elif members < 1_000:
        tips.append(
            f"👥 Растущий канал! Цель — 1 000+ подписчиков (сейчас {members:,})"
        )

    return min(score, 100), tips


def _generate_seo_fallback(
    title: str,
    about: str,
    username: str,
    keywords: list[str],
    preferred_username: str = "",
) -> dict:
    """Rule-based SEO suggestion when AI is not available."""
    kw_list = [k for k in keywords[:6] if k]
    kw_str = ", ".join(kw_list) if kw_list else ""
    keep_uname = preferred_username or username

    # Title: suggest adding keyword if too short
    new_title = title or ""
    if kw_list and len(new_title) < 12:
        candidate = (
            f"{new_title} — {kw_list[0]}" if new_title else kw_list[0].capitalize()
        )
        new_title = candidate[:50]

    # Description template
    if not about and kw_list:
        new_about = (
            f"{new_title} — канал о {kw_str}. "
            "Подписывайтесь и получайте актуальный контент! "
            f"Темы: {kw_str}. Присоединяйтесь к сообществу."
        )[:255]
    elif about and len(about) < 100 and kw_str:
        tail = f" Ключевые темы: {kw_str}."
        new_about = (about + tail)[:255]
    else:
        new_about = about

    return {
        "title": new_title,
        "about": new_about,
        "username": keep_uname,
        "reasoning": (
            "Базовая оптимизация по вашим ключевым словам. "
            "Для полной AI-генерации настройте OPENROUTER_API_KEY в Railway."
        ),
    }


async def _ai_generate_seo(
    http: aiohttp.ClientSession,
    title: str,
    about: str,
    username: str,
    entity_type: str,
    keywords: list[str],
    user_feedback: str = "",
    preferred_username: str = "",
) -> dict | None:
    """Generate SEO-optimized title, description, username via OpenRouter.

    user_feedback — правки от пользователя к предыдущему варианту.
    preferred_username — желаемый username (если пользователь указал).
    """
    try:
        from config import OPENROUTER_API_KEY, OPENROUTER_MODEL

        if not OPENROUTER_API_KEY:
            return None
    except ImportError:
        return None

    kw_hint = ", ".join(keywords[:10]) if keywords else "—"

    # Логика для username:
    # 1. Если пользователь указал конкретный — использовать его
    # 2. Если у канала уже есть username — не менять
    # 3. Если username нет — AI предлагает (но пользователь решит сам)
    actual_username = preferred_username or username
    if actual_username:
        username_rule = f'- username: MUST return exactly "{actual_username.lstrip("@")}" — do NOT change it'
        about_rule = (
            "- about: 150-255 chars, include 3-5 keywords naturally, ends with CTA"
        )
    else:
        username_rule = '- username: return empty string "" — do NOT suggest a username, user will set it separately'
        about_rule = (
            "- about: 150-255 chars, include 3-5 keywords naturally, ends with CTA"
        )

    feedback_section = ""
    if user_feedback:
        feedback_section = f"\nIMPORTANT USER CORRECTION: {user_feedback}\nYou MUST incorporate this feedback into the new version.\n"

    prompt = (
        f"You are a Telegram SEO expert. Optimize the following {entity_type} profile for maximum search visibility.\n\n"
        f"Current title: {title or '(empty)'}\n"
        f"Current description: {about[:200] if about else '(empty)'}\n"
        f"Current username: @{username if username else '(none)'}\n"
        f"Top user keywords: {kw_hint}\n"
        f"{feedback_section}\n"
        "Return ONLY valid JSON with these keys:\n"
        '{"title": "...", "about": "...", "username": "...", "reasoning": "..."}\n\n'
        "Rules:\n"
        "- title: max 50 chars, include main keyword, catchy\n"
        f"- {about_rule}\n"
        f"- {username_rule}\n"
        "- reasoning: 1-2 sentences explaining the strategy (in the same language as the profile)\n"
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
            locked_text("SEO-оптимизация канала", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
        )
        return
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id

    try:
        chan = await pool.fetchrow(
            "SELECT title, username, access_hash FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            callback.from_user.id,
        )
    except Exception:
        log.warning("seo chan_menu: failed to fetch channel row")
        chan = None
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await callback.answer()

    name = (
        f"@{chan['username']}"
        if chan.get("username")
        else html.escape(chan["title"] or "")
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Анализ SEO-скора",
        callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔍 Превью в поиске",
        callback_data=SeoCb(action="chan_preview", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🤖 AI-оптимизация",
        callback_data=SeoCb(action="chan_ai", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📊 Keyword Gap",
        callback_data=SeoCb(action="chan_content_gap", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔗 Альтернативы username",
        callback_data=SeoCb(action="chan_uname_alts", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="✏️ Применить изменения",
        callback_data=SeoCb(action="chan_apply", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📖 Гайд SEO 2026",
        callback_data=SeoCb(action="full_guide", bot_id=0, chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1, 2, 2, 2, 1)
    has_username = bool(chan.get("username"))
    username_hint = (
        f"🔗 Username: <b>@{html.escape(chan['username'])}</b> — учитывается в поиске"
        if has_username
        else "⚠️ <b>Username не задан</b> — это снижает видимость в поиске Telegram"
    )
    await callback.message.edit_text(
        f"📈 <b>SEO-оптимизация — {name}</b>\n\n"
        f"{username_hint}\n\n"
        "<b>Как Telegram ранжирует каналы:</b>\n"
        "1️⃣ <b>Username</b> (@handle) — прямое совпадение даёт топ-позицию\n"
        "2️⃣ <b>Название</b> — индексируется как заголовок\n"
        "3️⃣ <b>Описание</b> — ключевые слова повышают релевантность\n\n"
        "• <b>📊 Анализ</b> — SEO-скор по 4 критериям + советы\n"
        "• <b>🤖 AI-оптимизация</b> — ИИ напишет оптимальные title/about с вашим @username\n"
        "• <b>✏️ Применить</b> — обновить поля канала через аккаунт-администратор",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Channel SEO analysis ──────────────────────────────────────────


@router.callback_query(SeoCb.filter(F.action == "chan_analyze"))
async def cb_seo_chan_analyze(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO-анализ канала", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="analytics")
            ),
        )
        return
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id
    user_id = callback.from_user.id

    try:
        chan = await pool.fetchrow(
            "SELECT id, title, username, access_hash, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            user_id,
        )
    except Exception:
        log.warning("seo chan_analyze: failed to fetch channel row")
        chan = None
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await callback.answer()

    # Show progress message before the long Telethon call
    progress_msg = await callback.message.edit_text(
        "⏳ <b>Анализирую SEO...</b>\n\nПолучаю данные канала из Telegram...",
        parse_mode="HTML",
    )

    # Try to get full about from Telethon
    about = ""
    members = 0
    try:
        acc = (
            await pool.fetchrow(
                "SELECT session_str, device_model, system_version, app_version "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id,
                user_id,
            )
            if acc_id
            else None
        )
    except Exception:
        log.warning("seo chan_analyze: failed to fetch account row")
        acc = None

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

    # Save SEO score to history DB
    await db.save_seo_score(
        pool,
        owner_id=user_id,
        entity_type="channel",
        entity_id=chan_id,
        score=score,
        tips=tips,
    )

    display = f"@{username}" if username else html.escape(title)
    uname_line = (
        f"  🔗 Username: <b>@{html.escape(username)}</b> ✅ — ключевой фактор поиска"
        if username
        else "  🔗 Username: <b>⚠️ не задан</b> — без него канал почти не виден в поиске"
    )
    lines = [
        f"📊 <b>SEO-скор — {display}</b>\n",
        f"<b>{bar}</b>\n",
        "<b>Параметры (влияние на ранжирование):</b>",
        f"  📛 Название: <b>{html.escape(title)}</b>  ({len(title)}/50 симв.)",
        f"  📄 Описание: {len(about)}/255 симв."
        + (" ✅" if len(about) >= 150 else " ⚠️ мало"),
        uname_line,
        f"  👥 Подписчиков: {members:,}",
    ]
    if not username:
        lines.append(
            "\n💡 <b>Username критичен для SEO:</b> пользователи ищут каналы по @username "
            "и ключевым словам. Без username ваш канал не попадает в прямые результаты поиска."
        )
    if tips:
        lines.append("\n<b>Что улучшить:</b>")
        for t in tips:
            lines.append(f"  • {t}")
    if score >= 80:
        lines.append("\n✅ <b>Отличный SEO!</b> Канал хорошо виден в поиске.")
    elif score >= 50:
        lines.append("\n🟡 <b>Средний SEO.</b> Следуйте советам выше.")
    else:
        lines.append(
            "\n🔴 <b>Слабый SEO.</b> Приоритет: задать username + расширить описание."
        )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🤖 AI-оптимизация",
        callback_data=SeoCb(action="chan_ai", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📜 История проверок",
        callback_data=SeoCb(action="chan_history", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📋 Экспорт как текст",
        callback_data=SeoCb(action="chan_export_txt", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔄 Обновить анализ",
        callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    kb.adjust(2, 2, 1, 1)
    try:
        await progress_msg.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


# ── Export SEO report as plain text ──────────────────────────────


@router.callback_query(SeoCb.filter(F.action == "chan_export_txt"))
async def cb_seo_chan_export_txt(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    """Send SEO analysis as a plain-text message for easy copy-paste."""
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Экспорт SEO-анализа", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id
    user_id = callback.from_user.id

    try:
        chan = await pool.fetchrow(
            "SELECT id, title, username, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            user_id,
        )
    except Exception:
        log.warning("seo chan_export: failed to fetch channel row")
        chan = None
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await callback.answer("📋 Формирую отчёт...")

    title = chan["title"] or ""
    username = chan["username"] or ""
    display = f"@{username}" if username else title

    about = ""
    members = 0
    try:
        acc = (
            await pool.fetchrow(
                "SELECT session_str, device_model, system_version, app_version "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id,
                user_id,
            )
            if acc_id
            else None
        )
    except Exception:
        log.warning("seo chan_export: failed to fetch account row")
        acc = None

    if acc:
        try:
            from services import account_manager

            info = await account_manager.get_full_channel_info(
                acc["session_str"], chan["channel_id"], _acc=acc
            )
            if info:
                about = info.get("about", "") or ""
                members = info.get("members_count", 0) or 0
        except Exception as e:
            log.debug("chan_export_txt get_full_channel_info: %s", e)

    score, tips = _chan_seo_score(title, about, username, members)

    lines = [
        f"=== SEO-ОТЧЁТ: {display} ===",
        f"Дата: {__import__('datetime').date.today()}",
        "",
        f"SEO-скор: {score}/100",
        f"Название: {title} ({len(title)}/50 симв.)",
        f"Username: {'@' + username if username else '(не задан)'}",
        f"Описание: {len(about)}/255 симв.",
        f"Подписчиков: {members:,}",
    ]
    if tips:
        lines.append("")
        lines.append("Рекомендации:")
        for t in tips:
            # Strip emoji for plain text readability
            lines.append(f"  - {t}")
    if about:
        lines.append("")
        lines.append(f"Текущее описание ({len(about)} симв.):")
        lines.append(about[:512])

    report = "\n".join(lines)
    await callback.message.answer(
        f"<pre>{html.escape(report)}</pre>",
        parse_mode="HTML",
    )


# ── AI SEO generation ─────────────────────────────────────────────


@router.callback_query(SeoCb.filter(F.action == "chan_ai"))
async def cb_seo_chan_ai(
    callback: CallbackQuery,
    callback_data: SeoCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    state: FSMContext,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("AI SEO-оптимизация", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id
    user_id = callback.from_user.id

    try:
        chan = await pool.fetchrow(
            "SELECT title, username, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            user_id,
        )
    except Exception:
        log.warning("seo chan_ai: failed to fetch channel row")
        chan = None
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await callback.answer("🤖 Генерирую SEO-текст...")

    # Берём текущее описание через Telethon
    about = ""
    try:
        acc = (
            await pool.fetchrow(
                "SELECT session_str, device_model, system_version, app_version "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id,
                user_id,
            )
            if acc_id
            else None
        )
    except Exception:
        log.warning("seo chan_ai: failed to fetch account row")
        acc = None

    if acc:
        try:
            from services import account_manager

            info = await account_manager.get_full_channel_info(
                acc["session_str"], chan["channel_id"], _acc=acc
            )
            if info:
                about = info.get("about", "")
        except Exception:
            log_exc_swallow(
                log, "Не удалось получить about канала через get_full_channel_info"
            )

    # Ключевые слова из поисковой памяти
    try:
        kw_rows = await pool.fetch(
            "SELECT keyword FROM search_memory WHERE owner_id=$1 ORDER BY search_count DESC LIMIT 10",
            user_id,
        )
    except Exception:
        log.warning("seo chan_ai: failed to fetch search_memory keywords")
        kw_rows = []
    keywords = [r["keyword"] for r in kw_rows]

    # Получаем сохранённый фидбек и preferred_username из FSM (если это перегенерация)
    fsm_data = await state.get_data()
    user_feedback = (
        fsm_data.get("seo_feedback", "")
        if fsm_data.get("seo_chan_id") == chan_id
        else ""
    )
    preferred_username = (
        fsm_data.get("seo_preferred_username", "")
        if fsm_data.get("seo_chan_id") == chan_id
        else ""
    )

    result = await _ai_generate_seo(
        http,
        title=chan["title"] or "",
        about=about,
        username=chan["username"] or "",
        entity_type="Telegram channel",
        keywords=keywords,
        user_feedback=user_feedback,
        preferred_username=preferred_username,
    )

    if not result:
        # Fallback: rule-based suggestion when API key not configured
        result = _generate_seo_fallback(
            title=chan["title"] or "",
            about=about,
            username=chan["username"] or "",
            keywords=keywords,
            preferred_username=preferred_username,
        )

    new_title = result.get("title", "")
    new_about = result.get("about", "")
    new_username = result.get("username", "")  # Будет пустой если у канала нет username
    reasoning = result.get("reasoning", "")

    # Сохраняем предложение в БД
    try:
        await pool.execute(
            """INSERT INTO seo_ai_suggestions(owner_id, chan_id, title, about, username, created_at)
               VALUES($1,$2,$3,$4,$5,now())
               ON CONFLICT(owner_id, chan_id) DO UPDATE
               SET title=$3, about=$4, username=$5, created_at=now()""",
            user_id,
            chan_id,
            new_title,
            new_about,
            new_username,
        )
    except Exception:
        log_exc_swallow(
            log, "Не удалось сохранить SEO-предложение в сессионном AI-анализе"
        )

    # Сохраняем контекст в FSM для возможных правок
    await state.update_data(
        seo_chan_id=chan_id,
        seo_acc_id=acc_id,
        seo_feedback="",  # Сбрасываем после применения
    )
    await state.set_state(SeoFSM.waiting_feedback)

    # Формируем текст предложения
    lines = ["🤖 <b>AI-предложение по SEO</b>\n"]
    lines.append(f"📛 <b>Название:</b> {html.escape(new_title)}")
    lines.append(f"📄 <b>Описание:</b>\n<i>{html.escape(new_about)}</i>")
    if new_username:
        lines.append(f"🔗 <b>Username:</b> @{html.escape(new_username)}")
    else:
        lines.append("🔗 <b>Username:</b> <i>не изменится</i>")
    if reasoning:
        lines.append(f"\n💡 <i>{html.escape(reasoning)}</i>")
    lines.append("\n✏️ <i>Напишите правки текстом — я учту их при перегенерации</i>")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Применить всё",
        callback_data=SeoCb(action="apply_all", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📛 Только название",
        callback_data=SeoCb(action="apply_title", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📄 Только описание",
        callback_data=SeoCb(action="apply_about", chan_id=chan_id, acc_id=acc_id),
    )
    if new_username:
        kb.button(
            text="🔗 Только username",
            callback_data=SeoCb(action="apply_uname", chan_id=chan_id, acc_id=acc_id),
        )
    kb.button(
        text="🔤 Задать username",
        callback_data=SeoCb(action="ask_username", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔄 Перегенерировать",
        callback_data=SeoCb(action="chan_ai", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    if new_username:
        kb.adjust(1, 3, 2)
    else:
        kb.adjust(1, 2, 2)

    try:
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


# ── FSM: текстовый фидбек к AI-предложению ───────────────────────


@router.message(SeoFSM.waiting_feedback)
async def fsm_seo_feedback(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    """Пользователь написал правку — сохраняем и перегенерируем."""
    feedback = (message.text or "").strip()
    if not feedback:
        await message.answer("⚠️ Введите текст правки:")
        return

    data = await state.get_data()
    chan_id = data.get("seo_chan_id")
    acc_id = data.get("seo_acc_id", 0)

    if not chan_id:
        await state.clear()
        await message.answer("⚠️ Сессия SEO истекла. Откройте меню заново.")
        return

    # Накапливаем фидбек
    prev_feedback = data.get("seo_feedback", "")
    combined_feedback = (prev_feedback + "; " + feedback).strip("; ")
    await state.update_data(seo_feedback=combined_feedback)

    # Запускаем перегенерацию через callback simulation
    thinking = await message.answer(
        "🔄 <b>Обновляю SEO-предложение с учётом ваших правок...</b>", parse_mode="HTML"
    )

    # Получаем данные канала
    try:
        chan = await pool.fetchrow(
            "SELECT title, username, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            message.from_user.id,
        )
    except Exception:
        log.warning("seo feedback: failed to fetch channel row")
        chan = None
    if not chan:
        await thinking.edit_text("⚠️ Канал не найден. Откройте меню заново.")
        return

    try:
        acc = (
            await pool.fetchrow(
                "SELECT session_str, device_model, system_version, app_version "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id,
                message.from_user.id,
            )
            if acc_id
            else None
        )
    except Exception:
        log.warning("seo feedback: failed to fetch account row")
        acc = None

    about = ""
    if acc:
        try:
            from services import account_manager

            info = await account_manager.get_full_channel_info(
                acc["session_str"], chan["channel_id"], _acc=acc
            )
            if info:
                about = info.get("about", "")
        except Exception:
            log_exc_swallow(
                log,
                "Не удалось получить about канала через get_full_channel_info (обновление)",
            )

    try:
        kw_rows = await pool.fetch(
            "SELECT keyword FROM search_memory WHERE owner_id=$1 ORDER BY search_count DESC LIMIT 10",
            message.from_user.id,
        )
    except Exception:
        log.warning("seo feedback: failed to fetch search_memory keywords")
        kw_rows = []
    keywords = [r["keyword"] for r in kw_rows]
    preferred_username = data.get("seo_preferred_username", "")

    result = await _ai_generate_seo(
        http,
        title=chan["title"] or "",
        about=about,
        username=chan["username"] or "",
        entity_type="Telegram channel",
        keywords=keywords,
        user_feedback=combined_feedback,
        preferred_username=preferred_username,
    )

    if not result:
        result = _generate_seo_fallback(
            title=chan["title"] or "",
            about=about,
            username=chan["username"] or "",
            keywords=keywords,
            preferred_username=preferred_username,
        )

    new_title = result.get("title", "")
    new_about = result.get("about", "")
    new_username = result.get("username", "")
    reasoning = result.get("reasoning", "")

    try:
        await pool.execute(
            """INSERT INTO seo_ai_suggestions(owner_id, chan_id, title, about, username, created_at)
               VALUES($1,$2,$3,$4,$5,now())
               ON CONFLICT(owner_id, chan_id) DO UPDATE
               SET title=$3, about=$4, username=$5, created_at=now()""",
            message.from_user.id,
            chan_id,
            new_title,
            new_about,
            new_username,
        )
    except Exception:
        log_exc_swallow(log, "Не удалось сохранить SEO-предложение в БД (обновление)")

    lines = [
        "🤖 <b>AI-предложение (обновлено)</b>\n",
        f"📛 <b>Название:</b> {html.escape(new_title)}",
        f"📄 <b>Описание:</b>\n<i>{html.escape(new_about)}</i>",
    ]
    if new_username:
        lines.append(f"🔗 <b>Username:</b> @{html.escape(new_username)}")
    else:
        lines.append("🔗 <b>Username:</b> <i>не изменится</i>")
    if reasoning:
        lines.append(f"\n💡 <i>{html.escape(reasoning)}</i>")
    lines.append("\n✏️ <i>Ещё правки? Напишите их текстом</i>")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Применить всё",
        callback_data=SeoCb(action="apply_all", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📛 Только название",
        callback_data=SeoCb(action="apply_title", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📄 Только описание",
        callback_data=SeoCb(action="apply_about", chan_id=chan_id, acc_id=acc_id),
    )
    if new_username:
        kb.button(
            text="🔗 Только username",
            callback_data=SeoCb(action="apply_uname", chan_id=chan_id, acc_id=acc_id),
        )
    kb.button(
        text="🔤 Задать username",
        callback_data=SeoCb(action="ask_username", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔄 Перегенерировать",
        callback_data=SeoCb(action="chan_ai", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    kb.adjust(1, 2, 2)

    await thinking.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Задать желаемый username ───────────────────────────────────────


@router.callback_query(SeoCb.filter(F.action == "ask_username"))
async def cb_seo_ask_username(
    callback: CallbackQuery, callback_data: SeoCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(
        seo_chan_id=callback_data.chan_id, seo_acc_id=callback_data.acc_id
    )
    await state.set_state(SeoFSM.waiting_username)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⏭ Пропустить",
        callback_data=SeoCb(
            action="chan_ai", chan_id=callback_data.chan_id, acc_id=callback_data.acc_id
        ),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(
            action="chan_menu",
            chan_id=callback_data.chan_id,
            acc_id=callback_data.acc_id,
        ),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "🔤 <b>Желаемый username для канала</b>\n\n"
        "Введите username который хотите установить:\n"
        "<code>@my_channel</code> или <code>my_channel</code>\n\n"
        "Символ @ необязателен. Username должен:\n"
        "• Начинаться с буквы\n"
        "• Содержать только a–z, 0–9, _\n"
        "• Быть от 5 до 32 символов\n\n"
        "Если нажмёте «Пропустить» — AI не будет предлагать username.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(SeoFSM.waiting_username)
async def fsm_seo_username(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    import re

    uname = (message.text or "").strip().lstrip("@")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_]{4,31}$", uname):
        await message.answer(
            "⚠️ Некорректный username. Должен начинаться с буквы, содержать только a–z/0–9/_ и быть 5–32 символа:"
        )
        return

    data = await state.get_data()
    await state.update_data(seo_preferred_username=uname)
    await state.set_state(SeoFSM.waiting_feedback)

    chan_id = data.get("seo_chan_id", 0)
    acc_id = data.get("seo_acc_id", 0)

    await message.answer(
        f"✅ Username <code>@{html.escape(uname)}</code> запомнен.\n\n"
        "Перегенерирую предложение...",
        parse_mode="HTML",
    )

    # Имитируем нажатие кнопки "Перегенерировать"
    try:
        chan = await pool.fetchrow(
            "SELECT title, username, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            message.from_user.id,
        )
    except Exception:
        log.warning("seo preferred_username: failed to fetch channel row")
        chan = None
    if not chan:
        await message.answer("⚠️ Канал не найден.")
        return

    try:
        acc = (
            await pool.fetchrow(
                "SELECT session_str, device_model, system_version, app_version "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id,
                message.from_user.id,
            )
            if acc_id
            else None
        )
    except Exception:
        log.warning("seo preferred_username: failed to fetch account row")
        acc = None
    about = ""
    if acc:
        try:
            from services import account_manager

            info = await account_manager.get_full_channel_info(
                acc["session_str"], chan["channel_id"], _acc=acc
            )
            if info:
                about = info.get("about", "")
        except Exception:
            log_exc_swallow(
                log,
                "Не удалось получить about канала через get_full_channel_info (по username)",
            )

    try:
        kw_rows = await pool.fetch(
            "SELECT keyword FROM search_memory WHERE owner_id=$1 ORDER BY search_count DESC LIMIT 10",
            message.from_user.id,
        )
    except Exception:
        log.warning("seo preferred_username: failed to fetch search_memory keywords")
        kw_rows = []
    keywords = [r["keyword"] for r in kw_rows]
    user_feedback = (await state.get_data()).get("seo_feedback", "")

    result = await _ai_generate_seo(
        http,
        title=chan["title"] or "",
        about=about,
        username=chan["username"] or "",
        entity_type="Telegram channel",
        keywords=keywords,
        user_feedback=user_feedback,
        preferred_username=uname,
    )

    if not result:
        await state.clear()
        await message.answer("⚠️ AI недоступен. Проверьте OPENROUTER_API_KEY.")
        return

    new_title = result.get("title", "")
    new_about = result.get("about", "")
    reasoning = result.get("reasoning", "")

    try:
        await pool.execute(
            """INSERT INTO seo_ai_suggestions(owner_id, chan_id, title, about, username, created_at)
               VALUES($1,$2,$3,$4,$5,now())
               ON CONFLICT(owner_id, chan_id) DO UPDATE
               SET title=$3, about=$4, username=$5, created_at=now()""",
            message.from_user.id,
            chan_id,
            new_title,
            new_about,
            uname,
        )
    except Exception:
        log_exc_swallow(log, "Не удалось сохранить SEO-предложение в БД (по username)")

    lines = [
        f"🤖 <b>AI-предложение (username: @{html.escape(uname)})</b>\n",
        f"📛 <b>Название:</b> {html.escape(new_title)}",
        f"📄 <b>Описание:</b>\n<i>{html.escape(new_about)}</i>",
        f"🔗 <b>Username:</b> @{html.escape(uname)}",
    ]
    if reasoning:
        lines.append(f"\n💡 <i>{html.escape(reasoning)}</i>")
    lines.append("\n✏️ <i>Ещё правки? Напишите их текстом</i>")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Применить всё",
        callback_data=SeoCb(action="apply_all", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📛 Только название",
        callback_data=SeoCb(action="apply_title", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="📄 Только описание",
        callback_data=SeoCb(action="apply_about", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔗 Только username",
        callback_data=SeoCb(action="apply_uname", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔤 Изменить username",
        callback_data=SeoCb(action="ask_username", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="🔄 Перегенерировать",
        callback_data=SeoCb(action="chan_ai", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    kb.adjust(1, 3, 2)

    await message.answer(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Apply optimizations ───────────────────────────────────────────


async def _get_seo_suggestion(pool: asyncpg.Pool, user_id: int, chan_id: int) -> dict:
    """Retrieve last AI suggestion for a channel."""
    try:
        row = await pool.fetchrow(
            "SELECT title, about, username FROM seo_ai_suggestions WHERE owner_id=$1 AND chan_id=$2",
            user_id,
            chan_id,
        )
        return dict(row) if row else {}
    except Exception:
        return {}


async def _apply_chan_field(
    pool: asyncpg.Pool,
    user_id: int,
    chan_id: int,
    acc_id: int,
    field: str,
    value: str,
) -> tuple[bool, str]:
    """Apply a single field change to channel via Telethon."""
    try:
        chan = await pool.fetchrow(
            "SELECT channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            user_id,
        )
    except Exception:
        log.warning("_apply_chan_field: failed to fetch channel row")
        chan = None
    try:
        acc = (
            await pool.fetchrow(
                "SELECT session_str, device_model, system_version, app_version "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id,
                user_id,
            )
            if acc_id
            else None
        )
    except Exception:
        log.warning("_apply_chan_field: failed to fetch account row")
        acc = None

    if not chan or not acc:
        return False, "Канал или аккаунт не найден"

    from services import account_manager

    tg_chan_id = chan["channel_id"]

    if field == "title":
        ok = await account_manager.edit_channel_title(
            acc["session_str"], tg_chan_id, value, _acc=acc
        )
        if ok:
            try:
                await pool.execute(
                    "UPDATE managed_channels SET title=$1 WHERE id=$2", value, chan_id
                )
            except Exception:
                pass
        return ok, "" if ok else "Ошибка обновления названия"
    elif field == "about":
        ok = await account_manager.edit_channel_about(
            acc["session_str"], tg_chan_id, value, _acc=acc
        )
        if ok:
            try:
                await pool.execute(
                    "UPDATE managed_channels SET about=$1 WHERE id=$2", value, chan_id
                )
            except Exception:
                pass
        return ok, "" if ok else "Ошибка обновления описания"
    elif field == "username":
        err = await account_manager.set_channel_username(
            acc["session_str"], tg_chan_id, value, _acc=acc
        )
        if not err:
            try:
                await pool.execute(
                    "UPDATE managed_channels SET username=$1 WHERE id=$2", value, chan_id
                )
            except Exception:
                pass
        return not bool(err), err
    return False, "Неизвестное поле"


@router.callback_query(
    SeoCb.filter(
        F.action.in_(
            {"apply_all", "apply_title", "apply_about", "apply_uname", "chan_apply"}
        )
    )
)
async def cb_seo_apply(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO-оптимизация", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id
    user_id = callback.from_user.id
    action = callback_data.action

    if action == "chan_apply":
        await callback.answer()
        kb = InlineKeyboardBuilder()
        kb.button(
            text="📛 Изменить название",
            callback_data=SeoCb(action="edit_title", chan_id=chan_id, acc_id=acc_id),
        )
        kb.button(
            text="📄 Изменить описание",
            callback_data=SeoCb(action="edit_about", chan_id=chan_id, acc_id=acc_id),
        )
        kb.button(
            text="🔗 Изменить username",
            callback_data=SeoCb(action="edit_uname", chan_id=chan_id, acc_id=acc_id),
        )
        kb.button(
            text="◀️ Назад",
            callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
        )
        kb.adjust(1)
        await callback.message.edit_text(
            "✏️ <b>Применить SEO-изменения</b>\n\nВыберите что изменить прямо сейчас:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    suggestion = await _get_seo_suggestion(pool, user_id, chan_id)
    if not suggestion:
        await callback.answer("Сначала запустите AI-оптимизацию.", show_alert=True)
        return
    await callback.answer("⏳ Применяю...")

    results = []
    if action in ("apply_all", "apply_title") and suggestion.get("title"):
        ok, err = await _apply_chan_field(
            pool, user_id, chan_id, acc_id, "title", suggestion["title"]
        )
        results.append(f"📛 Название: {'✅' if ok else '❌ ' + err}")

    if action in ("apply_all", "apply_about") and suggestion.get("about"):
        ok, err = await _apply_chan_field(
            pool, user_id, chan_id, acc_id, "about", suggestion["about"]
        )
        results.append(f"📄 Описание: {'✅' if ok else '❌ ' + err}")

    if action in ("apply_all", "apply_uname") and suggestion.get("username"):
        ok, err = await _apply_chan_field(
            pool, user_id, chan_id, acc_id, "username", suggestion["username"]
        )
        results.append(f"🔗 Username: {'✅' if ok else '❌ ' + err}")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Новый анализ",
        callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            "<b>✏️ Результат применения SEO</b>\n\n"
            + "\n".join(results or ["Нечего применять."]),
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


# ── Ручное редактирование поля канала из SEO-меню ─────────────────────────

_SEO_EDIT_PROMPTS = {
    "edit_title": (
        "title",
        "📛 <b>Введите новое название канала</b> (до 128 символов):",
    ),
    "edit_about": (
        "about",
        "📄 <b>Введите новое описание канала</b> (до 255 символов):",
    ),
    "edit_uname": (
        "username",
        "🔗 <b>Введите новый username</b> (без @, 5–32 символа, a–z/0–9/_):",
    ),
}


@router.callback_query(
    SeoCb.filter(F.action.in_({"edit_title", "edit_about", "edit_uname"}))
)
async def cb_seo_edit_field(
    callback: CallbackQuery, callback_data: SeoCb, state: FSMContext
) -> None:
    await callback.answer()
    action = callback_data.action
    field, prompt = _SEO_EDIT_PROMPTS[action]
    await state.update_data(
        seo_edit_field=field,
        seo_chan_id=callback_data.chan_id,
        seo_acc_id=callback_data.acc_id,
    )
    await state.set_state(SeoFSM.waiting_edit_value)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Отмена",
        callback_data=SeoCb(
            action="chan_menu",
            chan_id=callback_data.chan_id,
            acc_id=callback_data.acc_id,
        ),
    )
    await callback.message.edit_text(
        prompt, parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.message(SeoFSM.waiting_edit_value)
async def fsm_seo_edit_value(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    value = (message.text or "").strip()
    sd = await state.get_data()
    field = sd.get("seo_edit_field", "title")
    chan_id = sd.get("seo_chan_id", 0)
    acc_id = sd.get("seo_acc_id", 0)

    if not value:
        await message.answer("⚠️ Значение не может быть пустым. Попробуйте ещё раз:")
        return

    if field == "title" and len(value) > 128:
        await message.answer("⚠️ Название не более 128 символов. Попробуйте ещё раз:")
        return
    if field == "about" and len(value) > 255:
        await message.answer("⚠️ Описание не более 255 символов. Попробуйте ещё раз:")
        return
    if field == "username":
        import re as _re

        value = value.lstrip("@")
        if not _re.match(r"^[a-zA-Z][a-zA-Z0-9_]{4,31}$", value):
            await message.answer(
                "⚠️ Некорректный username. Должен начинаться с буквы, содержать только a–z/0–9/_ и быть 5–32 символа:"
            )
            return

    await state.clear()
    ok, err = await _apply_chan_field(
        pool, message.from_user.id, chan_id, acc_id, field, value
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ К каналу",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    if ok:
        await message.answer(
            f"✅ <b>Обновлено успешно!</b>\nНовое значение: <code>{value[:100]}</code>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await message.answer(
            f"❌ <b>Ошибка обновления:</b> {err}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


# ══════════════════════════════════════════════════════════════════
# SEO ПОЛНЫЙ ГАЙД 2026 — реальные факторы ранжирования
# ══════════════════════════════════════════════════════════════════

_FULL_GUIDE_PAGES = [
    # Страница 1: Username + Название + Описание
    (
        "📖 <b>Реальные факторы SEO Telegram 2026</b>\n"
        "<i>Страница 1/3 — Профиль</i>\n\n"
        "🥇 <b>USERNAME — фактор №1 (вес ~35%)</b>\n"
        "Telegram ищет точное совпадение @username первым. Если пользователь ищет «vpn bot» и ваш username содержит «vpn» — вы в топ-3. Без username вы <b>невидимы</b> в прямом поиске по @.\n"
        "• Оптимально: 8–16 символов, ключевое слово + тема\n"
        "• Без цифр в конце — они снижают доверие алгоритма\n"
        "• Смена username сбрасывает накопленный «search authority» на ~2 недели\n\n"
        "🥈 <b>НАЗВАНИЕ / ИМЯ — фактор №2 (вес ~25%)</b>\n"
        "Telegram индексирует имя для полнотекстового поиска. 2–3 ключевых слова в начале названия — оптимум. Длина 20–40 символов.\n"
        "• ✅ «PDF Конвертер — Все форматы» → 3 сигнала\n"
        "• ❌ «Bot Helper Pro» → 0 тематических сигналов\n"
        "• Emoji в начале названия визуально выделяет в списке, но не влияет на ранг\n\n"
        "🥉 <b>ОПИСАНИЕ — фактор №3 (вес ~20%)</b>\n"
        "Полнотекстовый индекс. Telegram читает первые 255 символов для snippet в поиске.\n"
        "• Первые 2 предложения = ваш «meta description»\n"
        "• Включите: основное ключевое слово, 2–3 синонима, географию (если нужно), CTA\n"
        "• Повтор одного слова >4 раз = мягкий фильтр спама\n"
        "• Краткое описание (short_desc у ботов) = строчка под именем в поиске — заполните первым"
    ),
    # Страница 2: Вовлечённость + Активность + Скрытые факторы
    (
        "📖 <b>Реальные факторы SEO Telegram 2026</b>\n"
        "<i>Страница 2/3 — Сигналы вовлечённости</i>\n\n"
        "📊 <b>АУДИТОРИЯ И РОСТ (вес ~10%)</b>\n"
        "Важен не размер, а скорость роста за последние 30 дней. Канал с 500 подписчиками и +50/нед обгоняет канал с 10k и -100/нед.\n"
        "• Join/Leave ratio: если >30% уходят за месяц — ranking penalty\n"
        "• Premium-подписчики в вашей аудитории = повышенный trust signal\n\n"
        "🔥 <b>АКТИВНОСТЬ И СВЕЖЕСТЬ (вес ~8%)</b>\n"
        "Telegram понижает каналы без публикаций 30+ дней.\n"
        "• Для ботов: рассылки раз в неделю поддерживают «freshness score»\n"
        "• Для каналов: 3–5 постов в неделю — оптимум по алгоритму 2026\n"
        "• Пиковое время публикации (18–21 МСК) даёт больший охват в первый час\n\n"
        "💬 <b>ENGAGEMENT SIGNALS (скрытые)</b>\n"
        "• Reaction rate: >2% просмотров = сильный сигнал качества\n"
        "• Forward depth: если пост пересылают в 3+ каналов — massive boost\n"
        "• CTR из поиска: Telegram видит кто нажал на результат и кто нет. Слабый CTR снижает позицию.\n"
        "• Time-in-bot: для ботов — чем дольше сессия, тем выше retention score\n\n"
        "🌐 <b>BACKLINKS TELEGRAM-STYLE</b>\n"
        "Telegram анализирует упоминания вашего @username в других публичных каналах.\n"
        "• Упоминание в канале с 10k+ подписчиков = «backlink высокого DA»\n"
        "• Взаимопиар с каналами из вашей ниши — легитимный способ роста authority\n"
        "• Нативные форварды ценнее прямых @mentions"
    ),
    # Страница 3: Продвинутые и малоизвестные факторы
    (
        "📖 <b>Реальные факторы SEO Telegram 2026</b>\n"
        "<i>Страница 3/3 — Продвинутые факторы</i>\n\n"
        "🔬 <b>ФАКТОРЫ, О КОТОРЫХ НЕ ГОВОРЯТ</b>\n\n"
        "🏷 <b>Языковая консистентность</b>\n"
        "Telegram определяет язык канала и показывает его носителям этого языка. Смешанный контент снижает языковой confidence — канал перестаёт приоритизироваться в обоих сегментах.\n\n"
        "🗺 <b>Геотаргетинг через username</b>\n"
        'Пользователи ищут «доставка москва», «spb news», «dubai crypto». Username с geo-частью ("_msk", "_spb", "dubai") напрямую попадает в локальные запросы.\n\n'
        "⚡ <b>Fragment / TON эффект</b>\n"
        "Каналы с зарезервированным @username на Fragment.com получают дополнительный verification signal. Telegram проверяет право собственности.\n\n"
        "🤖 <b>Для ботов: Bot API quality score</b>\n"
        "Telegram внутренне оценивает ботов по паттернам API-запросов:\n"
        "• Быстрый ответ (&lt;2с) = качественный бот\n"
        "• Частые ошибки/таймауты = снижение видимости\n"
        "• Inline mode с хорошим CTR = отдельный позитивный сигнал\n\n"
        "📌 <b>Pinned message как SEO-элемент</b>\n"
        "В некоторых контекстах поиска Telegram показывает первое закреплённое сообщение как preview. Сделайте его keyword-rich.\n\n"
        "🔢 <b>Hashtag-индексирование</b>\n"
        "Посты с хэштегами индексируются отдельно. #тема → канал попадает в поиск по хэштегу. 1–3 тематических хэштега на пост — оптимум.\n\n"
        "⚠️ <b>Антипаттерны (штрафы):</b>\n"
        "• Keyword stuffing (>5 повторов слова) → мягкий фильтр\n"
        "• Накрутка подписчиков ботами → trust collapse\n"
        "• Массовые жалобы → временное скрытие из поиска\n"
        "• Частая смена username → потеря search authority"
    ),
]


@router.callback_query(
    SeoCb.filter(F.action.in_({"full_guide", "full_guide_p2", "full_guide_p3"}))
)
async def cb_seo_full_guide(callback: CallbackQuery, callback_data: SeoCb) -> None:
    await callback.answer()
    page_map = {"full_guide": 0, "full_guide_p2": 1, "full_guide_p3": 2}
    page = page_map.get(callback_data.action, 0)
    text = _FULL_GUIDE_PAGES[page]
    next_actions = ["full_guide_p2", "full_guide_p3", None]
    next_labels = ["▶️ Стр. 2: Вовлечённость →", "▶️ Стр. 3: Продвинутые факторы →", None]
    bot_id = callback_data.bot_id
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id

    kb = InlineKeyboardBuilder()
    if next_actions[page]:
        kb.button(
            text=next_labels[page],
            callback_data=SeoCb(
                action=next_actions[page], bot_id=bot_id, chan_id=chan_id, acc_id=acc_id
            ),
        )
    if page > 0:
        prev = ["full_guide", "full_guide", "full_guide_p2"][page]
        kb.button(
            text=f"◀️ Стр. {page}",
            callback_data=SeoCb(action=prev, bot_id=bot_id, chan_id=chan_id, acc_id=acc_id),
        )
    if bot_id:
        back_cb = SeoCb(action="menu", bot_id=bot_id)
    elif chan_id:
        back_cb = SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id)
    else:
        back_cb = SeoCb(action="menu", bot_id=0)
    kb.button(text="🏠 К SEO-меню", callback_data=back_cb)
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            raise


# ══════════════════════════════════════════════════════════════════
# ПОИСКОВЫЙ PREVIEW — как вы выглядите в поиске Telegram
# ══════════════════════════════════════════════════════════════════


@router.callback_query(SeoCb.filter(F.action == "preview"))
async def cb_seo_preview(
    callback: CallbackQuery,
    callback_data: SeoCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO-превью", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    try:
        row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    except Exception:
        await callback.answer("Ошибка базы данных.", show_alert=True)
        return
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer("🔍 Генерирую превью...")
    token = row["token"]
    name, short_desc = await asyncio.gather(
        bot_api.get_my_name(http, token),
        bot_api.get_my_short_description(http, token),
        return_exceptions=True,
    )
    if isinstance(name, Exception):
        name = row.get("first_name") or "Бот"
    if isinstance(short_desc, Exception):
        short_desc = ""

    uname = row.get("username") or ""
    try:
        audience = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM bot_users WHERE bot_id=$1", callback_data.bot_id
            )
            or 0
        )
    except Exception:
        log.warning("seo preview: failed to fetch audience count")
        audience = 0

    uname_line = f"@{uname}" if uname else "⚠️ username не задан"
    aud_str = f"{int(audience):,}" if int(audience) >= 1000 else str(int(audience))
    short_preview = (
        (short_desc[:72] + "…") if len(short_desc or "") > 72 else (short_desc or "")
    )

    score, _ = _seo_score(name or "", "", short_desc or "", [], int(audience), uname)
    bar = _score_bar(score)

    lines = [
        "🔍 <b>Так вы выглядите в поиске Telegram:</b>\n",
        "┌─────────────────────────────────────┐",
        f"│ 🤖 <b>{html.escape((name or 'Бот')[:32])}</b>",
        f"│ {html.escape(uname_line)} · {aud_str} users",
    ]
    if short_preview:
        lines.append(f"│ <i>{html.escape(short_preview)}</i>")
    else:
        lines.append("│ <i>⚠️ Краткое описание не заполнено</i>")
    lines += ["└─────────────────────────────────────┘\n", f"SEO-скор: {bar}\n"]

    issues = []
    if not uname:
        issues.append("❌ Нет username — кнопка @... не отображается")
    if not short_desc:
        issues.append("❌ Нет краткого описания — третья строка пустая")
    elif len(short_desc) < 40:
        issues.append(
            f"⚠️ Краткое описание короткое ({len(short_desc)}/120) — добавьте ключевые слова"
        )
    if not name or len(name) < 5:
        issues.append("❌ Короткое имя — добавьте тематические слова")
    if not issues:
        issues.append("✅ Карточка выглядит хорошо!")
    lines.append("<b>Что улучшить:</b>")
    for i in issues:
        lines.append(f"  {i}")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✏️ Редактировать профиль",
        callback_data=EditCb(action="menu", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=callback_data.bot_id)
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(SeoCb.filter(F.action == "chan_preview"))
async def cb_seo_chan_preview(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("SEO-превью канала", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    chan_id = callback_data.chan_id
    try:
        chan = await pool.fetchrow(
            "SELECT title, username FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            callback.from_user.id,
        )
    except Exception:
        log.warning("seo chan_preview: failed to fetch channel row")
        chan = None
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await callback.answer()

    title = chan.get("title") or "Канал"
    uname = chan.get("username") or ""
    uname_line = f"@{uname}" if uname else "⚠️ без @username"
    lines = [
        "🔍 <b>Так канал выглядит в поиске Telegram:</b>\n",
        "┌─────────────────────────────────────┐",
        f"│ 📢 <b>{html.escape(title[:40])}</b>",
        f"│ {html.escape(uname_line)} · N подписчиков",
        "│ <i>← первые 100 симв. описания (About)</i>",
        "└─────────────────────────────────────┘\n",
        "<b>Что влияет на эту карточку:</b>",
        "  1️⃣ Название — содержит ли ключевые слова?",
        f"  2️⃣ @username — {'✅ задан' if uname else '❌ НЕ ЗАДАН — критично!'}",
        "  3️⃣ About — первые 100 символов = snippet в поиске",
        "  4️⃣ Подписчики — влияют на доверие при клике",
    ]
    if not uname:
        lines.append(
            "\n💡 <b>Без username</b> канал не появляется при поиске по @имени "
            "и теряет ~35% потенциального поискового трафика."
        )
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🤖 AI-оптимизация",
        callback_data=SeoCb(
            action="chan_ai", chan_id=chan_id, acc_id=callback_data.acc_id
        ),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(
            action="chan_menu", chan_id=chan_id, acc_id=callback_data.acc_id
        ),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ══════════════════════════════════════════════════════════════════
# MOMENTUM — динамика позиций по ключевым словам
# ══════════════════════════════════════════════════════════════════


@router.callback_query(SeoCb.filter(F.action == "momentum"))
async def cb_seo_momentum(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Динамика позиций", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    bot_id = callback_data.bot_id
    try:
        row = await db.get_bot(pool, bot_id, callback.from_user.id)
    except Exception:
        await callback.answer("Ошибка базы данных.", show_alert=True)
        return
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()

    keywords = await db.get_tracked_keywords(pool, bot_id)
    if not keywords:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=bot_id))
        await callback.message.edit_text(
            "📈 <b>Нет данных о позициях</b>\n\n"
            "Добавьте ключевые слова в разделе /menu → 📊 Аналитика → 🔍 Ключевые слова. "
            "После первых проверок здесь появится динамика.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    label = (
        f"@{row.get('username')}"
        if row.get("username")
        else row.get("first_name", str(bot_id))
    )
    lines = [f"📈 <b>Динамика позиций — {label}</b>\n"]
    total_up = total_down = total_same = 0

    for kw in keywords:
        history = await db.get_ranking_history(
            pool, kw["id"], limit=5, owner_id=callback.from_user.id
        )
        if not history or len(history) < 2:
            pos_now = history[0]["position"] if history else "—"
            lines.append(
                f"  ⚪→ <code>{html.escape(kw['keyword'])[:18]:<18}</code> #{pos_now} (мало данных)"
            )
            continue
        pos_now = history[0]["position"]
        pos_prev = history[-1]["position"]
        if pos_now is None:
            arrow, diff_str = "⚪", "нет данных"
        elif pos_prev is None:
            arrow, diff_str = "🆕", f"#{pos_now}"
        elif pos_now < pos_prev:
            arrow, diff_str = "🟢↑", f"#{pos_now} (+{pos_prev - pos_now})"
            total_up += 1
        elif pos_now > pos_prev:
            arrow, diff_str = "🔴↓", f"#{pos_now} (-{pos_now - pos_prev})"
            total_down += 1
        else:
            arrow, diff_str = "⚪→", f"#{pos_now}"
            total_same += 1
        lines.append(
            f"  {arrow} <code>{html.escape(kw['keyword'])[:18]:<18}</code> {diff_str}"
        )

    if total_up or total_down or total_same:
        lines.append(
            f"\n📊 🟢 Растут: {total_up} | 🔴 Падают: {total_down} | ⚪ Стабильны: {total_same}"
        )
        if total_up > total_down:
            lines.append("💪 Хороший momentum! Оптимизация работает.")
        elif total_down > total_up:
            lines.append("⚠️ Позиции падают — обновите описание и повысьте активность.")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=bot_id))
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ══════════════════════════════════════════════════════════════════
# KEYWORD GAP — анализ покрытия ключевых слов в описании
# ══════════════════════════════════════════════════════════════════


def _keyword_coverage(text: str, keywords: list[str]) -> list[tuple[str, bool]]:
    text_lower = text.lower()
    return [(kw, all(w in text_lower for w in kw.lower().split())) for kw in keywords]


def _keyword_density(text: str, keywords: list[str]) -> list[tuple[str, int]]:
    """Return how many times each keyword appears in text (word-level count).

    Counts non-overlapping occurrences of all words of the keyword phrase.
    For single-word keywords this is equivalent to word frequency.
    """
    if not text:
        return [(kw, 0) for kw in keywords]
    # Split text into lowercase words (remove punctuation)
    import re as _re
    words = _re.findall(r"[a-zа-яёa-z0-9_]+", text.lower())
    word_freq: dict[str, int] = {}
    for w in words:
        word_freq[w] = word_freq.get(w, 0) + 1

    result = []
    for kw in keywords:
        kw_words = _re.findall(r"[a-zа-яёa-z0-9_]+", kw.lower())
        if not kw_words:
            result.append((kw, 0))
            continue
        # For single words use exact count; for phrases use minimum word count
        counts = [word_freq.get(w, 0) for w in kw_words]
        result.append((kw, min(counts)))
    return result


@router.callback_query(SeoCb.filter(F.action == "content_gap"))
async def cb_seo_content_gap(
    callback: CallbackQuery,
    callback_data: SeoCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Keyword Gap анализ", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    bot_id = callback_data.bot_id
    try:
        row = await db.get_bot(pool, bot_id, callback.from_user.id)
    except Exception:
        await callback.answer("Ошибка базы данных.", show_alert=True)
        return
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer("🔍 Анализирую покрытие...")
    token = row["token"]
    name, description, short_desc = await asyncio.gather(
        bot_api.get_my_name(http, token),
        bot_api.get_my_description(http, token),
        bot_api.get_my_short_description(http, token),
        return_exceptions=True,
    )
    if isinstance(name, Exception):
        name = row.get("first_name") or ""
    if isinstance(description, Exception):
        description = ""
    if isinstance(short_desc, Exception):
        short_desc = ""

    user_kws = await db.get_top_keywords(pool, bot_id, limit=15)
    tracked = await db.get_tracked_keywords(pool, bot_id)
    all_text = f"{name} {description} {short_desc}"
    combined = list(
        dict.fromkeys(
            [r["keyword"] for r in user_kws] + [r["keyword"] for r in tracked]
        )
    )[:20]

    if not combined:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=bot_id))
        await callback.message.edit_text(
            "📊 <b>Нет данных для анализа</b>\n\nНакопите ключевые слова пользователей или добавьте keywords в Visibility.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    coverage = _keyword_coverage(all_text, combined)
    density = dict(_keyword_density(all_text, combined))
    found = [kw for kw, ok in coverage if ok]
    missing = [kw for kw, ok in coverage if not ok]
    pct = round(len(found) / len(coverage) * 100) if coverage else 0
    label = (
        f"@{row.get('username')}"
        if row.get("username")
        else row.get("first_name", str(bot_id))
    )

    lines = [
        f"📊 <b>Keyword Gap — {label}</b>\n",
        f"Покрытие: <b>{pct}%</b> ({len(found)}/{len(coverage)} слов в профиле)\n",
    ]
    if missing:
        lines.append("<b>❌ Не представлены в описании (добавьте!):</b>")
        for kw in missing[:10]:
            lines.append(f"  • <code>{html.escape(kw)}</code>")
    if found:
        lines.append(f"\n<b>✅ Уже покрыты ({len(found)}) — плотность:</b>")
        for kw in found[:6]:
            cnt = density.get(kw, 0)
            bar_len = min(cnt, 5)
            bar = "█" * bar_len + "░" * (5 - bar_len)
            lines.append(f"  • <code>{html.escape(kw)}</code> {bar} ×{cnt}")
    if missing:
        lines.append(
            "\n💡 Добавьте отсутствующие слова в описание → больший охват в поиске."
        )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✏️ Редактировать профиль",
        callback_data=EditCb(action="menu", bot_id=bot_id),
    )
    kb.button(text="◀️ Назад", callback_data=SeoCb(action="menu", bot_id=bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(SeoCb.filter(F.action == "chan_content_gap"))
async def cb_seo_chan_content_gap(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Keyword Gap — канал", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id
    user_id = callback.from_user.id

    try:
        chan = await pool.fetchrow(
            "SELECT title, username, channel_id FROM managed_channels WHERE id=$1 AND owner_id=$2",
            chan_id,
            user_id,
        )
    except Exception:
        log.warning("seo chan_content_gap: failed to fetch channel row")
        chan = None
    if not chan:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await callback.answer()

    about = ""
    if acc_id:
        try:
            acc = await pool.fetchrow(
                "SELECT session_str, device_model, system_version, app_version "
                "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
                acc_id,
                user_id,
            )
        except Exception:
            log.warning("seo chan_content_gap: failed to fetch account row")
            acc = None
        if acc:
            try:
                from services import account_manager

                info = await account_manager.get_full_channel_info(
                    acc["session_str"], chan["channel_id"], _acc=acc
                )
                if info:
                    about = info.get("about", "") or ""
            except Exception:
                log_exc_swallow(
                    log, "Не удалось получить about канала для анализа ключевых слов"
                )

    all_text = f"{chan.get('title', '')} {chan.get('username', '')} {about}"
    try:
        tracked = await pool.fetch(
            "SELECT keyword FROM tracked_keywords WHERE owner_id=$1 AND is_active=TRUE LIMIT 20",
            user_id,
        )
    except Exception:
        log.warning("seo chan_content_gap: failed to fetch tracked keywords")
        tracked = []
    kw_list = [r["keyword"] for r in tracked]

    if not kw_list:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад",
            callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
        )
        await callback.message.edit_text(
            "📊 <b>Нет ключевых слов для сравнения</b>\n\n"
            "Добавьте отслеживаемые ключевые слова через 📊 Аналитика → Ключевые слова.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    coverage = _keyword_coverage(all_text, kw_list)
    density = dict(_keyword_density(all_text, kw_list))
    found = [kw for kw, ok in coverage if ok]
    missing = [kw for kw, ok in coverage if not ok]
    pct = round(len(found) / len(coverage) * 100) if coverage else 0
    name_d = (
        f"@{chan['username']}"
        if chan.get("username")
        else html.escape(chan.get("title", ""))
    )

    lines = [
        f"📊 <b>Keyword Gap — {name_d}</b>\n",
        f"Покрытие: <b>{pct}%</b> ({len(found)}/{len(coverage)})\n",
    ]
    if missing:
        lines.append("<b>❌ Не найдены в названии/описании:</b>")
        for kw in missing[:10]:
            lines.append(f"  • <code>{html.escape(kw)}</code>")
    if found:
        lines.append(f"\n<b>✅ Присутствуют ({len(found)}) — плотность:</b>")
        for kw in found[:6]:
            cnt = density.get(kw, 0)
            bar_len = min(cnt, 5)
            bar = "█" * bar_len + "░" * (5 - bar_len)
            lines.append(f"  • <code>{html.escape(kw)}</code> {bar} ×{cnt}")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🤖 AI — добавить в описание",
        callback_data=SeoCb(action="chan_ai", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ══════════════════════════════════════════════════════════════════
# USERNAME ALTERNATIVES — предложения лучших username
# ══════════════════════════════════════════════════════════════════


@router.callback_query(SeoCb.filter(F.action.in_({"uname_alts", "chan_uname_alts"})))
async def cb_seo_uname_alts(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Альтернативы username", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="analytics")),
        )
        return
    is_chan = callback_data.action == "chan_uname_alts"

    if is_chan:
        try:
            chan = await pool.fetchrow(
                "SELECT title, username FROM managed_channels WHERE id=$1 AND owner_id=$2",
                callback_data.chan_id,
                callback.from_user.id,
            )
        except Exception:
            log.warning("seo uname_alts: failed to fetch channel row")
            chan = None
        if not chan:
            await callback.answer("Не найдено.", show_alert=True)
            return
        current = chan.get("username") or ""
        base = current or chan.get("title") or "channel"
        back_cb = SeoCb(
            action="chan_menu",
            chan_id=callback_data.chan_id,
            acc_id=callback_data.acc_id,
        )
    else:
        try:
            row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
        except Exception:
            await callback.answer("Ошибка базы данных.", show_alert=True)
            return
        if not row:
            await callback.answer("Не найдено.", show_alert=True)
            return
        current = row.get("username") or ""
        base = current or row.get("first_name") or "bot"
        back_cb = SeoCb(action="menu", bot_id=callback_data.bot_id)
    await callback.answer()

    from services.username_engine import generate_username_variants

    variants = generate_username_variants(base, geo=None)
    all_variants = [v for v in variants if v != current][:8]

    current_esc = f"@{html.escape(current)}" if current else "не задан"
    lines = [
        "🔗 <b>Альтернативные username для SEO</b>\n",
        f"Текущий: <b>{current_esc}</b>\n",
        "<b>Варианты (от лучшего):</b>",
    ]
    stars = ["⭐⭐⭐", "⭐⭐⭐", "⭐⭐", "⭐⭐", "⭐", "⭐", "⭐", "⭐"]
    for i, v in enumerate(all_variants):
        s = stars[i] if i < len(stars) else "⭐"
        lines.append(f"  <code>@{html.escape(v)}</code> {s} ({len(v)} симв.)")

    lines += [
        "\n<b>💡 Принципы хорошего username для SEO:</b>",
        "• 8–14 символов — оптимум для поиска",
        "• Ключевое слово в начале (не в конце)",
        "• Без цифр в конце — выглядит органически",
        "• Гео-суффикс (_msk, _spb, _dubai) = локальный трафик",
        "• Смена сбрасывает search authority на ~2 нед.",
        "\n🔍 Проверить доступность: введите @username в поиске Telegram",
    ]

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=back_cb)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ══════════════════════════════════════════════════════════════════
# SEO SCORE HISTORY — per-entity check log
# ══════════════════════════════════════════════════════════════════


@router.callback_query(SeoCb.filter(F.action == "bot_history"))
async def cb_seo_bot_history(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    """Show SEO check history for a bot."""
    await callback.answer()
    bot_id = callback_data.bot_id
    history = await db.get_seo_score_history(
        pool, callback.from_user.id, "bot", bot_id, limit=10
    )
    if not history:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=SeoCb(action="analyze", bot_id=bot_id))
        await callback.message.edit_text(
            "📜 <b>История SEO-проверок</b>\n\nЕщё нет ни одной проверки. Запустите анализ.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines = ["📜 <b>История SEO-проверок бота</b>\n"]
    for rec in history:
        ts = rec["checked_at"].strftime("%d.%m.%y %H:%M")
        bar = _score_bar(rec["score"])
        lines.append(f"  [{ts}] {bar}")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Новый анализ",
        callback_data=SeoCb(action="analyze", bot_id=bot_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="menu", bot_id=bot_id),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(SeoCb.filter(F.action == "chan_history"))
async def cb_seo_chan_history(
    callback: CallbackQuery, callback_data: SeoCb, pool: asyncpg.Pool
) -> None:
    """Show SEO check history for a channel."""
    await callback.answer()
    chan_id = callback_data.chan_id
    acc_id = callback_data.acc_id
    history = await db.get_seo_score_history(
        pool, callback.from_user.id, "channel", chan_id, limit=10
    )
    if not history:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад",
            callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id),
        )
        await callback.message.edit_text(
            "📜 <b>История SEO-проверок канала</b>\n\nЕщё нет ни одной проверки. Запустите анализ.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    lines = ["📜 <b>История SEO-проверок канала</b>\n"]
    # Show trend: compare consecutive scores
    scores = [r["score"] for r in history]
    for i, rec in enumerate(history):
        ts = rec["checked_at"].strftime("%d.%m.%y %H:%M")
        sc = rec["score"]
        if i + 1 < len(scores):
            prev = scores[i + 1]
            if sc > prev:
                trend = "↗️"
            elif sc < prev:
                trend = "↘️"
            else:
                trend = "→"
        else:
            trend = "🆕"
        lines.append(f"  [{ts}] {_score_bar(sc)} {trend}")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Новый анализ",
        callback_data=SeoCb(action="chan_analyze", chan_id=chan_id, acc_id=acc_id),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=SeoCb(action="chan_menu", chan_id=chan_id, acc_id=acc_id),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )

"""SEO score analyzer and keyword analytics for managed bots."""
from __future__ import annotations
import asyncio
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg
from bot.callbacks import SeoCb, EditCb, BotCb
from bot.keyboards import seo_menu
from database import db
from services import bot_api

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
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"📈 <b>SEO / Аналитика — {label}</b>\n\n"
        "Анализируйте профиль бота и отслеживайте что пишут пользователи "
        "— используйте эти данные для улучшения позиций в поиске Telegram.",
        parse_mode="HTML",
        reply_markup=seo_menu(callback_data.bot_id),
    )
    await callback.answer()


@router.callback_query(SeoCb.filter(F.action == "analyze"))
async def cb_seo_analyze(callback: CallbackQuery, callback_data: SeoCb,
                          pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
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
        lines.append(f"\n<b>Что улучшить:</b>")
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
    await callback.answer()


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

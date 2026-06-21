"""
Ad Intelligence Hub — разведка рекламного рынка Telegram.

BotMother сканирует рекламные посты в каналах, строит базу данных
рекламодателей и плейсментов, AI оценивает качество аудитории vs
накрутку, даёт рекомендации где размещаться.

Flows:
- Главный дашборд: каналы в базе, средний quality score, активные рекламодатели
- Добавить канал для анализа (@username → сканируем рекламу)
- Топ каналов по quality_score с ценами и ER
- Рекомендации для размещения (с бюджетом)
- Активные рекламодатели в нише (кто рекламируется, у кого)
- Рыночный отчёт: средняя цена рекламы, топ-10 каналов
"""

from __future__ import annotations

import asyncio
import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AdIntelCb, BmCb

log = logging.getLogger(__name__)
router = Router(name="ad_intelligence_hub")

_PAGE_SIZE = 10


# ── FSM ──────────────────────────────────────────────────────────────────


class AdIntelFSM(StatesGroup):
    waiting_channel = State()
    waiting_budget = State()
    waiting_niche = State()


# ── Клавиатуры ───────────────────────────────────────────────────────────


def _main_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить канал", callback_data=AdIntelCb(action="add_channel"))
    kb.button(text="🏆 Топ каналов", callback_data=AdIntelCb(action="top_channels"))
    kb.button(text="💡 Рекомендации", callback_data=AdIntelCb(action="recommendations"))
    kb.button(text="🔍 Рекламодатели", callback_data=AdIntelCb(action="advertisers"))
    kb.button(text="📊 Рыночный отчёт", callback_data=AdIntelCb(action="market_report"))
    kb.button(text="◀️ Меню", callback_data=BmCb(action="analytics"))
    kb.adjust(1)
    return kb


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=AdIntelCb(action="menu"))
    return kb


def _cancel_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AdIntelCb(action="menu"))
    return kb


# ── Форматирование ────────────────────────────────────────────────────────


def _quality_bar(score: float) -> str:
    """Визуальная полоска качества."""
    filled = int(score / 10)
    return "█" * filled + "░" * (10 - filled)


def _fmt_num(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _fmt_er(er: float) -> str:
    pct = er * 100 if er <= 1.0 else er
    return f"{pct:.1f}%"


def _quality_label(score: float) -> str:
    if score >= 75:
        return "🟢 Отлично"
    if score >= 50:
        return "🟡 Хорошо"
    if score >= 25:
        return "🟠 Средне"
    return "🔴 Слабо"


# ── Главное меню ──────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "menu"))
async def cb_adi_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()

    from services.ad_intelligence import get_dashboard_stats

    try:
        stats = await get_dashboard_stats(pool, callback.from_user.id)
    except Exception:
        log.exception("cb_adi_menu: get_dashboard_stats error")
        stats = {
            "total_channels": 0,
            "avg_quality": 0.0,
            "recently_scanned": 0,
            "advertisers_30d": 0,
            "total_ad_posts": 0,
        }

    text = (
        "📡 <b>Ad Intelligence — Рекламная разведка</b>\n\n"
        f"📦 Каналов в базе: <b>{stats['total_channels']}</b>\n"
        f"⭐ Средний quality score: <b>{stats['avg_quality']:.1f}/100</b>\n"
        f"🔄 Отсканировано за 7 дней: <b>{stats['recently_scanned']}</b>\n"
        f"👥 Активных рекламодателей (30д): <b>{stats['advertisers_30d']}</b>\n"
        f"📝 Всего рекламных постов: <b>{stats['total_ad_posts']}</b>\n\n"
        "<i>Сканируй каналы, находи рекламодателей, оценивай качество аудитории.</i>"
    )

    kb = _main_kb()
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Команда входа ─────────────────────────────────────────────────────────


@router.message(Command("ad_intel"))
async def cmd_ad_intel(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    await state.clear()

    from services.ad_intelligence import get_dashboard_stats

    try:
        stats = await get_dashboard_stats(pool, message.from_user.id)
    except Exception:
        stats = {
            "total_channels": 0,
            "avg_quality": 0.0,
            "recently_scanned": 0,
            "advertisers_30d": 0,
            "total_ad_posts": 0,
        }

    text = (
        "📡 <b>Ad Intelligence — Рекламная разведка</b>\n\n"
        f"📦 Каналов в базе: <b>{stats['total_channels']}</b>\n"
        f"⭐ Средний quality score: <b>{stats['avg_quality']:.1f}/100</b>\n"
        f"🔄 Отсканировано за 7 дней: <b>{stats['recently_scanned']}</b>\n"
        f"👥 Активных рекламодателей (30д): <b>{stats['advertisers_30d']}</b>\n"
        f"📝 Всего рекламных постов: <b>{stats['total_ad_posts']}</b>\n\n"
        "<i>Сканируй каналы, находи рекламодателей, оценивай качество аудитории.</i>"
    )

    kb = _main_kb()
    await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Добавить канал ────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "add_channel"))
async def cb_adi_add_channel(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AdIntelFSM.waiting_channel)

    text = (
        "➕ <b>Добавить канал для анализа</b>\n\n"
        "Отправь <code>@username</code> канала или ссылку <code>t.me/channelname</code>.\n\n"
        "Бот просканирует последние 100 постов, найдёт рекламу и "
        "оценит качество аудитории."
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb().as_markup())


@router.message(AdIntelFSM.waiting_channel)
async def msg_adi_channel_input(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    raw = (message.text or "").strip()
    # Нормализуем: @username или t.me/username → username
    username = raw.lstrip("@").strip()
    if "t.me/" in username:
        username = username.split("t.me/")[-1].split("/")[0].strip()
    if not username or len(username) < 3:
        await message.answer(
            "⚠️ Неверный формат. Отправь <code>@username</code> или "
            "<code>t.me/channelname</code>.",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        return

    await state.clear()

    # Получаем аккаунт для сканирования
    async with pool.acquire() as conn:
        acc = await conn.fetchrow(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE LIMIT 1",
            message.from_user.id,
        )

    if not acc:
        await message.answer(
            "⚠️ Нет активных аккаунтов Telegram в пуле.\n"
            "Добавь аккаунт в разделе <b>Аккаунты</b> для сканирования каналов.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    status_msg = await message.answer(
        f"🔍 Сканирую <b>@{html.escape(username)}</b>...\n"
        "<i>Читаю последние 100 постов, ищу рекламу...</i>",
        parse_mode="HTML",
    )

    from services.ad_intelligence import scan_channel_ads

    try:
        result = await asyncio.wait_for(
            scan_channel_ads(pool, username, acc["id"], message.from_user.id),
            timeout=60,
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text(
            "⏱ Превышено время ожидания при сканировании канала.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return
    except Exception as exc:
        log.exception("msg_adi_channel_input: scan_channel_ads error")
        await status_msg.edit_text(
            f"❌ Ошибка при сканировании: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    if result.get("status") == "error":
        await status_msg.edit_text(
            f"❌ Не удалось просканировать канал.\n"
            f"<code>{html.escape(result.get('error', 'Неизвестная ошибка'))}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    qs = result.get("quality_score", 0.0)
    ad_posts = result.get("ad_posts_found", 0)
    subs = result.get("subscribers", 0)
    title = result.get("channel_title", username)

    text = (
        f"✅ <b>@{html.escape(username)}</b> — сканирование завершено\n\n"
        f"📺 Название: <b>{html.escape(title)}</b>\n"
        f"👥 Подписчиков: <b>{_fmt_num(subs)}</b>\n"
        f"⭐ Quality Score: <b>{qs:.1f}/100</b> {_quality_label(qs)}\n"
        f"{_quality_bar(qs)}\n\n"
        f"📝 Найдено рекламных постов: <b>{ad_posts}</b>\n\n"
        "<i>Канал добавлен в базу Ad Intelligence.</i>"
    )

    kb = InlineKeyboardBuilder()
    placement_id = result.get("placement_id", 0)
    if placement_id:
        kb.button(
            text="🔎 Детали канала",
            callback_data=AdIntelCb(action="placement_detail", placement_id=placement_id),
        )
    kb.button(text="➕ Добавить ещё", callback_data=AdIntelCb(action="add_channel"))
    kb.button(text="◀️ Дашборд", callback_data=AdIntelCb(action="menu"))
    kb.adjust(1)
    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Топ каналов ───────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "top_channels"))
async def cb_adi_top_channels(
    callback: CallbackQuery,
    callback_data: AdIntelCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    page = callback_data.page
    offset = page * _PAGE_SIZE

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM ad_placements WHERE owner_id=$1",
            callback.from_user.id,
        )
        rows = await conn.fetch(
            """
            SELECT channel_username, channel_title, subscribers,
                   views_avg, er_rate, ad_price_est, quality_score, ad_posts_count
            FROM ad_placements
            WHERE owner_id = $1
            ORDER BY quality_score DESC
            LIMIT $2 OFFSET $3
            """,
            callback.from_user.id,
            _PAGE_SIZE,
            offset,
        )

    if not rows:
        await callback.message.edit_text(
            "📭 Каналов в базе пока нет.\n\n"
            "Добавь первый канал для анализа через <b>Добавить канал</b>.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = [f"🏆 <b>Топ каналов по качеству</b> (стр. {page+1})\n"]
    for i, r in enumerate(rows, start=offset + 1):
        qs = r["quality_score"]
        er = _fmt_er(r["er_rate"])
        price = r["ad_price_est"]
        subs = _fmt_num(r["subscribers"])
        uname = html.escape(r["channel_username"])
        title = html.escape(r["channel_title"] or r["channel_username"])
        lines.append(
            f"{i}. <b>{title}</b> (@{uname})\n"
            f"   👥 {subs} · ER {er} · ⭐ {qs:.0f}/100\n"
            f"   💰 ~{price:,} ⭐ за пост · 📝 {r['ad_posts_count']} рекл. постов\n"
        )

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(
            text="◀️ Предыдущие",
            callback_data=AdIntelCb(action="top_channels", page=page - 1),
        )
    if (offset + _PAGE_SIZE) < (total or 0):
        kb.button(
            text="Следующие ▶️",
            callback_data=AdIntelCb(action="top_channels", page=page + 1),
        )
    kb.button(text="◀️ Дашборд", callback_data=AdIntelCb(action="menu"))
    kb.adjust(2, 1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Детали канала ─────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "placement_detail"))
async def cb_adi_placement_detail(
    callback: CallbackQuery,
    callback_data: AdIntelCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT channel_username, channel_title, subscribers, views_avg,
                   er_rate, ad_price_est, quality_score, niches,
                   ad_posts_count, last_ad_seen_at, last_scanned_at
            FROM ad_placements
            WHERE id=$1 AND owner_id=$2
            """,
            callback_data.placement_id,
            callback.from_user.id,
        )

    if not r:
        await callback.answer("Канал не найден", show_alert=True)
        return

    qs = r["quality_score"]
    niches_str = ", ".join(r["niches"]) if r["niches"] else "не определены"
    last_ad = r["last_ad_seen_at"].strftime("%d.%m.%Y") if r["last_ad_seen_at"] else "нет данных"
    scanned = r["last_scanned_at"].strftime("%d.%m.%Y %H:%M") if r["last_scanned_at"] else "?"

    text = (
        f"📺 <b>{html.escape(r['channel_title'] or r['channel_username'])}</b>\n"
        f"@{html.escape(r['channel_username'])}\n\n"
        f"👥 Подписчиков: <b>{_fmt_num(r['subscribers'])}</b>\n"
        f"👁 Средние просмотры: <b>{_fmt_num(r['views_avg'])}</b>\n"
        f"📈 ER: <b>{_fmt_er(r['er_rate'])}</b>\n"
        f"⭐ Quality Score: <b>{qs:.1f}/100</b> {_quality_label(qs)}\n"
        f"{_quality_bar(qs)}\n\n"
        f"💰 Оценка цены рекламы: <b>{r['ad_price_est']:,} Stars</b>\n"
        f"📝 Рекламных постов найдено: <b>{r['ad_posts_count']}</b>\n"
        f"🗓 Последняя реклама: <b>{last_ad}</b>\n"
        f"🔄 Сканирование: <b>{scanned}</b>\n"
        f"🏷 Ниши: <i>{html.escape(niches_str)}</i>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Ресканировать",
        callback_data=AdIntelCb(
            action="rescan_channel", placement_id=callback_data.placement_id
        ),
    )
    kb.button(text="◀️ К топу", callback_data=AdIntelCb(action="top_channels"))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Ресканировать канал ───────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "rescan_channel"))
async def cb_adi_rescan_channel(
    callback: CallbackQuery,
    callback_data: AdIntelCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("Запускаю сканирование...")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT channel_username FROM ad_placements WHERE id=$1 AND owner_id=$2",
            callback_data.placement_id,
            callback.from_user.id,
        )
        acc = await conn.fetchrow(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE LIMIT 1",
            callback.from_user.id,
        )

    if not row:
        await callback.answer("Канал не найден", show_alert=True)
        return

    if not acc:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов для сканирования.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    await callback.message.edit_text(
        f"🔍 Ресканирую <b>@{html.escape(row['channel_username'])}</b>...",
        parse_mode="HTML",
    )

    from services.ad_intelligence import scan_channel_ads

    try:
        result = await asyncio.wait_for(
            scan_channel_ads(pool, row["channel_username"], acc["id"], callback.from_user.id),
            timeout=60,
        )
    except asyncio.TimeoutError:
        await callback.message.edit_text(
            "⏱ Таймаут при сканировании.", parse_mode="HTML", reply_markup=_back_kb().as_markup()
        )
        return
    except Exception as exc:
        await callback.message.edit_text(
            f"❌ Ошибка: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    if result.get("status") == "error":
        await callback.message.edit_text(
            f"❌ {html.escape(result.get('error', 'Ошибка'))}",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    qs = result.get("quality_score", 0.0)
    ad_posts = result.get("ad_posts_found", 0)
    await callback.message.edit_text(
        f"✅ Ресканирование завершено!\n\n"
        f"⭐ Quality Score: <b>{qs:.1f}/100</b> {_quality_label(qs)}\n"
        f"📝 Рекламных постов найдено: <b>{ad_posts}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder()
        .button(
            text="🔎 Обновлённые детали",
            callback_data=AdIntelCb(
                action="placement_detail", placement_id=callback_data.placement_id
            ),
        )
        .button(text="◀️ Дашборд", callback_data=AdIntelCb(action="menu"))
        .adjust(1)
        .as_markup(),
    )


# ── Рекомендации ─────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "recommendations"))
async def cb_adi_recommendations(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AdIntelFSM.waiting_budget)

    await callback.message.edit_text(
        "💡 <b>Рекомендации для размещения рекламы</b>\n\n"
        "Укажи бюджет в Stars (Telegram) или отправь <code>0</code> для "
        "рекомендаций без ограничения бюджета.\n\n"
        "<i>Например: 5000</i>",
        parse_mode="HTML",
        reply_markup=_cancel_kb().as_markup(),
    )


@router.message(AdIntelFSM.waiting_budget)
async def msg_adi_budget_input(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", "")
    try:
        budget = int(raw)
        if budget < 0:
            raise ValueError("negative")
    except (ValueError, TypeError):
        await message.answer(
            "⚠️ Введи число (количество Stars) или <code>0</code> без ограничения.",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        return

    await state.clear()

    from services.ad_intelligence import get_recommendations

    try:
        recs = await get_recommendations(pool, message.from_user.id, budget_stars=budget)
    except Exception as exc:
        log.exception("msg_adi_budget_input: get_recommendations error")
        await message.answer(
            f"❌ Ошибка при получении рекомендаций: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    if not recs:
        budget_str = f"до {budget:,} Stars" if budget > 0 else "без ограничения бюджета"
        await message.answer(
            f"📭 Нет рекомендаций ({budget_str}).\n\n"
            "Добавь больше каналов в базу для получения рекомендаций.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    budget_str = f"до {budget:,} ⭐" if budget > 0 else "без ограничения"
    lines = [f"💡 <b>Рекомендации для размещения</b> ({budget_str})\n"]

    for i, rec in enumerate(recs[:10], 1):
        qs = rec["quality_score"]
        roi = rec["roi_score"]
        price = rec["ad_price_est"]
        er = _fmt_er(rec["er_rate"])
        subs = _fmt_num(rec["subscribers"])
        uname = html.escape(rec["channel_username"])
        title = html.escape(rec["channel_title"] or rec["channel_username"])

        lines.append(
            f"{i}. <b>{title}</b> (@{uname})\n"
            f"   👥 {subs} · ER {er} · ⭐ {qs:.0f}/100\n"
            f"   💰 ~{price:,} Stars · ROI-индекс: <b>{roi:.1f}</b>\n"
        )

    if len(recs) > 10:
        lines.append(f"\n<i>...и ещё {len(recs)-10} каналов</i>")

    lines.append("\n<i>ROI-индекс: quality_score / (цена/1000). Выше = выгоднее.</i>")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )


# ── Активные рекламодатели ────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "advertisers"))
async def cb_adi_advertisers(
    callback: CallbackQuery,
    callback_data: AdIntelCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()

    page = callback_data.page
    offset = page * _PAGE_SIZE

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            """
            SELECT COUNT(*) FROM ad_advertisers
            WHERE owner_id=$1
              AND last_seen_at >= NOW() - INTERVAL '30 days'
            """,
            callback.from_user.id,
        )
        rows = await conn.fetch(
            """
            SELECT advertiser_username, niche, placements_count, last_seen_at
            FROM ad_advertisers
            WHERE owner_id=$1
              AND last_seen_at >= NOW() - INTERVAL '30 days'
            ORDER BY placements_count DESC, last_seen_at DESC
            LIMIT $2 OFFSET $3
            """,
            callback.from_user.id,
            _PAGE_SIZE,
            offset,
        )

    if not rows:
        await callback.message.edit_text(
            "📭 Активных рекламодателей за последние 30 дней не найдено.\n\n"
            "Сканируй каналы, чтобы собирать данные о рекламодателях.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = [f"🔍 <b>Активные рекламодатели (30 дней)</b> — стр. {page+1}\n"]
    for i, r in enumerate(rows, start=offset + 1):
        uname = html.escape(r["advertiser_username"])
        niche = html.escape(r["niche"] or "неизвестно")
        last_seen = r["last_seen_at"].strftime("%d.%m") if r["last_seen_at"] else "?"
        lines.append(
            f"{i}. <b>@{uname}</b>\n"
            f"   🏷 Ниша: {niche} · 📢 размещений: {r['placements_count']} · последний: {last_seen}\n"
        )

    text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(
            text="◀️ Назад",
            callback_data=AdIntelCb(action="advertisers", page=page - 1),
        )
    if (offset + _PAGE_SIZE) < (total or 0):
        kb.button(
            text="Вперёд ▶️",
            callback_data=AdIntelCb(action="advertisers", page=page + 1),
        )
    kb.button(text="◀️ Дашборд", callback_data=AdIntelCb(action="menu"))
    kb.adjust(2, 1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Рыночный отчёт ────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "market_report"))
async def cb_adi_market_report(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AdIntelFSM.waiting_niche)

    await callback.message.edit_text(
        "📊 <b>Рыночный отчёт</b>\n\n"
        "Введи нишу для фильтрации (например: <code>крипто</code>, <code>новости</code>)\n"
        "или отправь <code>-</code> для общего отчёта по всем каналам.",
        parse_mode="HTML",
        reply_markup=_cancel_kb().as_markup(),
    )


@router.message(AdIntelFSM.waiting_niche)
async def msg_adi_niche_input(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    raw = (message.text or "").strip()
    niche = "" if raw in ("-", ".", "все", "all") else raw.lower()

    await state.clear()

    from services.ad_intelligence import get_market_report

    try:
        report = await get_market_report(pool, message.from_user.id, niche=niche)
    except Exception as exc:
        log.exception("msg_adi_niche_input: get_market_report error")
        await message.answer(
            f"❌ Ошибка при получении отчёта: <code>{html.escape(str(exc)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    niche_label = f" в нише «{html.escape(niche)}»" if niche else ""
    lines = [f"📊 <b>Рыночный отчёт{niche_label}</b>\n"]

    lines.append(f"📦 Каналов проанализировано: <b>{report['total_channels']}</b>")
    lines.append(f"⭐ Средний quality score: <b>{report['avg_quality']}/100</b>")
    lines.append(
        f"💰 Средняя цена рекламы: <b>{report['avg_price']:,} Stars</b>"
    )
    lines.append(f"📝 Всего рекламных постов: <b>{report['total_ad_posts']}</b>\n")

    if report["top_channels"]:
        lines.append("<b>🏆 Топ-10 каналов по качеству:</b>")
        for i, ch in enumerate(report["top_channels"][:10], 1):
            uname = html.escape(ch["channel_username"])
            qs = ch["quality_score"]
            er = _fmt_er(ch["er_rate"])
            subs = _fmt_num(ch["subscribers"])
            price = ch["ad_price_est"]
            lines.append(
                f"{i}. @{uname} · 👥{subs} · ER {er} · "
                f"⭐{qs:.0f} · 💰{price:,}⭐"
            )

    if report["active_advertisers"]:
        lines.append(f"\n<b>👥 Активные рекламодатели (30д):</b>")
        for adv in report["active_advertisers"][:10]:
            uname = html.escape(adv["advertiser_username"])
            cnt = adv["placements_count"]
            lines.append(f"• @{uname} — {cnt} размещений")

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )

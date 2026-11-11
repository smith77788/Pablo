"""
Ad Intelligence Hub — разведка рекламного рынка Telegram.

Infragram сканирует рекламные посты в каналах, строит базу данных
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
from bot.utils.op_helpers import safe_answer

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
    await safe_answer(callback)
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
        "📡 <b>Разведка рекламы</b>\n\n"
        f"📦 Каналов в базе: <b>{stats['total_channels']}</b>\n"
        f"⭐ Среднее качество каналов: <b>{stats['avg_quality']:.1f}/100</b>\n"
        f"🔄 Отсканировано за 7 дней: <b>{stats['recently_scanned']}</b>\n"
        f"👥 Активных рекламодателей (30д): <b>{stats['advertisers_30d']}</b>\n"
        f"📝 Всего рекламных постов: <b>{stats['total_ad_posts']}</b>\n\n"
        "<i>Добавьте чужой канал — разведка найдёт его рекламодателей, оценит охват, вовлечённость и цену поста и будет обновлять данные раз в сутки. Полный экран с фильтрами — в мини-аппе.</i>"
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
        "📡 <b>Разведка рекламы</b>\n\n"
        f"📦 Каналов в базе: <b>{stats['total_channels']}</b>\n"
        f"⭐ Среднее качество каналов: <b>{stats['avg_quality']:.1f}/100</b>\n"
        f"🔄 Отсканировано за 7 дней: <b>{stats['recently_scanned']}</b>\n"
        f"👥 Активных рекламодателей (30д): <b>{stats['advertisers_30d']}</b>\n"
        f"📝 Всего рекламных постов: <b>{stats['total_ad_posts']}</b>\n\n"
        "<i>Добавьте чужой канал — разведка найдёт его рекламодателей, оценит охват, вовлечённость и цену поста и будет обновлять данные раз в сутки. Полный экран с фильтрами — в мини-аппе.</i>"
    )

    kb = _main_kb()
    await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Добавить канал ────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "add_channel"))
async def cb_adi_add_channel(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await safe_answer(callback)
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
    from services.ad_intelligence import channel_username_from
    username = channel_username_from(raw)
    if not username:
        await message.answer(
            "⚠️ Неверный формат. Отправь <code>@username</code> или "
            "<code>t.me/channelname</code>.",
            parse_mode="HTML",
            reply_markup=_cancel_kb().as_markup(),
        )
        return

    await state.clear()

    # Скан идёт ОПЕРАЦИЕЙ, а не прямо из обработчика: прямой вызов брал
    # «первый активный» аккаунт без захвата под операцию, то есть мог поднять
    # вторую сессию на auth-key, уже занятый другой операцией
    # (AUTH_KEY_DUPLICATED), и держал обработчик бота до минуты.
    await _queue_scan(message, pool, message.from_user.id, username)


async def _queue_scan(target: Message, pool: asyncpg.Pool, owner_id: int,
                      username: str, *, edit: bool = False) -> None:
    from services import operation_bus as _obus
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить ещё", callback_data=AdIntelCb(action="add_channel"))
    kb.button(text="◀️ Дашборд", callback_data=AdIntelCb(action="menu"))
    kb.adjust(1)
    try:
        op_id = await _obus.submit(
            pool, owner_id, "ad_intel_scan", {"channel": username},
            total_items=1, label=f"Разведка рекламы @{username}")
    except PermissionError as exc:
        from bot.utils.owner_error import owner_error
        text = f"⚠️ {owner_error(exc)}"
        op_id = 0
    except Exception:
        log.exception("ad_intel: постановка скана @%s не удалась", username)
        text = "⚠️ Не удалось поставить сканирование в очередь. Повторите позже."
        op_id = 0
    if op_id:
        text = (
            f"🔍 Сканирование <b>@{html.escape(username)}</b> поставлено в очередь "
            f"(операция №{op_id}).\n\n"
            "Разведка прочитает последние 100 постов, найдёт рекламу и "
            "рекламодателей, оценит охват, вовлечённость и цену поста. Обычно "
            "это занимает до минуты; итог придёт в уведомлении об операции, а "
            "канал появится в «Топе каналов». Дальше он будет обновляться сам "
            "раз в сутки."
        )
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Топ каналов ───────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "top_channels"))
async def cb_adi_top_channels(
    callback: CallbackQuery,
    callback_data: AdIntelCb,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)

    page = callback_data.page
    offset = page * _PAGE_SIZE

    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM ad_placements WHERE owner_id=$1",
            callback.from_user.id,
        )
        rows = await conn.fetch(
            """
            SELECT id, channel_username, channel_title, subscribers,
                   views_avg, er_rate, ad_price_est, quality_score, ad_posts_count
            FROM ad_placements
            WHERE owner_id = $1
            ORDER BY quality_score DESC, id DESC
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
    kb = InlineKeyboardBuilder()
    for i, r in enumerate(rows, start=offset + 1):
        qs = r["quality_score"]
        er = _fmt_er(r["er_rate"])
        price = r["ad_price_est"]
        subs = _fmt_num(r["subscribers"])
        uname = html.escape(r["channel_username"])
        title = html.escape(r["channel_title"] or r["channel_username"])
        lines.append(
            f"{i}. <b>{title}</b> (@{uname})\n"
            f"   👥 {subs} · охват {er} · качество {qs:.0f}/100\n"
            f"   💰 ~{price:,} ₽ за пост · 📝 {r['ad_posts_count']} рекл. постов\n"
        )
        # Кликабельная кнопка деталей на каждый канал (handler placement_detail уже есть)
        kb.button(
            text=f"🔎 {i}. {(r['channel_title'] or r['channel_username'])[:24]}",
            callback_data=AdIntelCb(action="placement_detail", placement_id=r["id"]),
        )

    text = "\n".join(lines)

    n_nav = 0
    if page > 0:
        kb.button(
            text="◀️ Предыдущие",
            callback_data=AdIntelCb(action="top_channels", page=page - 1),
        )
        n_nav += 1
    if (offset + _PAGE_SIZE) < (total or 0):
        kb.button(
            text="Следующие ▶️",
            callback_data=AdIntelCb(action="top_channels", page=page + 1),
        )
        n_nav += 1
    kb.button(text="◀️ Дашборд", callback_data=AdIntelCb(action="menu"))
    # Layout: каждый канал-деталь на своей строке, навигация вместе, дашборд отдельно
    _layout = [1] * len(rows)
    if n_nav:
        _layout.append(n_nav)
    _layout.append(1)
    kb.adjust(*_layout)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Детали канала ─────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "placement_detail"))
async def cb_adi_placement_detail(
    callback: CallbackQuery,
    callback_data: AdIntelCb,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)

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
        f"📈 Охват поста: <b>{_fmt_er(r['er_rate'])}</b> от подписчиков\n"
        f"⭐ Качество: <b>{qs:.1f}/100</b> {_quality_label(qs)}\n"
        f"{_quality_bar(qs)}\n\n"
        f"💰 Оценка цены поста: <b>~{r['ad_price_est']:,} ₽</b>\n"
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
    await callback.answer("Ставлю сканирование в очередь…")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT channel_username FROM ad_placements WHERE id=$1 AND owner_id=$2",
            callback_data.placement_id,
            callback.from_user.id,
        )

    if not row:
        await callback.answer("Канал не найден", show_alert=True)
        return

    await _queue_scan(callback.message, pool, callback.from_user.id,
                      row["channel_username"], edit=True)


# ── Рекомендации ─────────────────────────────────────────────────────────


@router.callback_query(AdIntelCb.filter(F.action == "recommendations"))
async def cb_adi_recommendations(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await safe_answer(callback)
    await state.set_state(AdIntelFSM.waiting_budget)

    await callback.message.edit_text(
        "💡 <b>Рекомендации для размещения рекламы</b>\n\n"
        "Укажи бюджет на один пост в рублях или отправь <code>0</code> для "
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
            "⚠️ Введи сумму в рублях или <code>0</code> без ограничения.",
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
        budget_str = f"до {budget:,} ₽" if budget > 0 else "без ограничения бюджета"
        await message.answer(
            f"📭 Нет рекомендаций ({budget_str}).\n\n"
            "Добавь больше каналов в базу для получения рекомендаций.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    budget_str = f"до {budget:,} ₽" if budget > 0 else "без ограничения"
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
            f"   👥 {subs} · охват {er} · качество {qs:.0f}/100\n"
            f"   💰 ~{price:,} ₽ · индекс выгоды: <b>{roi:.1f}</b>\n"
        )

    if len(recs) > 10:
        lines.append(f"\n<i>...и ещё {len(recs)-10} каналов</i>")

    lines.append("\n<i>Индекс выгоды: качество ÷ (цена ÷ 1000). Выше — выгоднее.</i>")

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
    await safe_answer(callback)

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
            ORDER BY placements_count DESC, last_seen_at DESC, id DESC
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
    await safe_answer(callback)
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
    lines.append(f"⭐ Среднее качество каналов: <b>{report['avg_quality']}/100</b>")
    lines.append(
        f"💰 Средняя цена поста: <b>~{report['avg_price']:,} ₽</b>"
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
                f"{i}. @{uname} · 👥{subs} · охват {er} · "
                f"⭐{qs:.0f} · 💰~{price:,} ₽"
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

"""
DM Campaigns — управление прямыми рассылками подписчикам.

Аудитория: только собственные подписчики ботов и CRM-контакты.
Entry: DmCb(action="menu")
"""
from __future__ import annotations

import asyncio
import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import DmCb, BotCb, BmCb
from bot.states import DmCampaignFSM
from services import task_registry as _treg
from services.logger import log_exc_swallow
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup

log = logging.getLogger(__name__)
router = Router()

_STATUS_EMOJI = {
    "draft": "📝",
    "running": "▶️",
    "paused": "⏸️",
    "done": "✅",
    "failed": "❌",
}
_STATUS_LABELS = {
    "draft": "черновик",
    "running": "выполняется",
    "paused": "на паузе",
    "done": "завершена",
    "failed": "ошибка",
}


async def _edit(callback: CallbackQuery, text: str, markup=None) -> None:
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=markup
        )
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)


# ── Menu ──────────────────────────────────────────────────────────────────────

@router.callback_query(DmCb.filter(F.action == "menu"))
async def cb_dm_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.answer()
        await _edit(callback, locked_text("DM-кампании", "enterprise"), subscription_locked_markup("enterprise", back_callback=BmCb(action="main")))
        return
    await callback.answer()

    campaigns = await pool.fetch(
        "SELECT id, name, status, sent_count, fail_count, total_targets, created_at "
        "FROM dm_campaigns WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 10",
        callback.from_user.id,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новая кампания", callback_data=DmCb(action="new"))
    for c in campaigns:
        icon = _STATUS_EMOJI.get(c["status"], "❓")
        total = c["total_targets"] or 0
        sent = c["sent_count"] or 0
        label = f"{icon} {c['name'][:22]} ({sent}/{total})"
        kb.button(text=label, callback_data=DmCb(action="detail", campaign_id=c["id"]))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)

    count = len(campaigns)
    text = (
        "<b>📨 DM-кампании</b>\n\n"
        "Отправляйте персонализированные сообщения своим подписчикам.\n\n"
        "📌 <i>Аудитория: подписчики ваших ботов и CRM-контакты.</i>\n"
        f"Активных кампаний: <b>{count}</b>"
    )
    await _edit(callback, text, kb.as_markup())


# ── Create — Step 1: name ─────────────────────────────────────────────────────

@router.callback_query(DmCb.filter(F.action == "new"))
async def cb_dm_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(DmCampaignFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
    await callback.message.answer(
        "📨 <b>Новая DM-кампания</b>\n\n"
        "Введите <b>название</b> кампании:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(DmCampaignFSM.waiting_name)
async def fsm_dm_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Введите название:")
        return
    await state.update_data(dm_name=name)
    await state.set_state(DmCampaignFSM.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
    await message.answer(
        f"✅ Название: <b>{html.escape(name)}</b>\n\n"
        "📝 Введите <b>текст сообщения</b>.\n\n"
        "💡 Поддерживается спинтакс: <code>{Привет|Здравствуйте|Добрый день}</code>\n"
        "Каждый получатель увидит случайный вариант.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(DmCampaignFSM.waiting_text)
async def fsm_dm_text(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("⚠️ Текст не может быть пустым:")
        return
    await state.update_data(dm_text=text.strip())
    await state.set_state(DmCampaignFSM.choosing_target)

    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Все подписчики бота", callback_data=DmCb(action="target_type", campaign_id=0))
    kb.button(text="🎯 По когорте (активность)", callback_data=DmCb(action="target_cohort_bot"))
    kb.button(text="👥 CRM-контакты",           callback_data=DmCb(action="target_crm"))
    kb.button(text="❌ Отмена",                 callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "👥 <b>Выберите аудиторию</b>:\n\n"
        "🎯 <b>По когорте</b> — отправить только активным/неактивным пользователям:\n"
        "  • 🔥 Hot — активны за последние 24ч\n"
        "  • 🟡 Warm — активны 1-7 дней назад\n"
        "  • 🧊 Cold — активны 7-30 дней назад\n"
        "  • 💀 Lost — неактивны более 30 дней",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Create — Step 3a: choose bot for bot_users ────────────────────────────────

@router.callback_query(DmCb.filter(F.action == "target_type"))
async def cb_dm_target_bot(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.update_data(dm_target_type="bot_users")
    await state.set_state(DmCampaignFSM.choosing_bot)

    bots = await pool.fetch(
        "SELECT bot_id, first_name, username FROM managed_bots "
        "WHERE added_by=$1 AND is_active=true ORDER BY first_name LIMIT 20",
        callback.from_user.id,
    )

    kb = InlineKeyboardBuilder()
    for b in bots:
        label = b.get("first_name") or b.get("username") or f"bot_{b['bot_id']}"
        kb.button(text=f"🤖 {label[:28]}", callback_data=DmCb(action="target_bot_id", campaign_id=b["bot_id"]))
    kb.button(text="◀️ Назад", callback_data=DmCb(action="new"))
    kb.adjust(1)

    if not bots:
        await _edit(callback, "⚠️ У вас нет активных ботов.", kb.as_markup())
        return
    await _edit(callback, "🤖 Выберите бот (откуда брать подписчиков):", kb.as_markup())


@router.callback_query(DmCb.filter(F.action == "target_bot_id"))
async def cb_dm_target_bot_selected(
    callback: CallbackQuery,
    callback_data: DmCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bot_id = callback_data.campaign_id
    await state.update_data(dm_target_id=bot_id)
    await _show_dm_preview(callback, state, pool)


# ── Create — Step 3c: Cohort targeting ───────────────────────────────────────

@router.callback_query(DmCb.filter(F.action == "target_cohort_bot"))
async def cb_dm_target_cohort_bot(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Step 1: pick bot for cohort targeting."""
    await callback.answer()
    bots = await pool.fetch(
        "SELECT bot_id, first_name, username FROM managed_bots "
        "WHERE added_by=$1 AND is_active=true ORDER BY first_name LIMIT 20",
        callback.from_user.id,
    )
    if not bots:
        await callback.message.edit_text(
            "⚠️ У вас нет активных ботов.",
            parse_mode="HTML",
        )
        return

    kb = InlineKeyboardBuilder()
    for b in bots:
        label = b.get("first_name") or b.get("username") or f"bot_{b['bot_id']}"
        kb.button(
            text=f"🤖 {label[:28]}",
            callback_data=DmCb(action="target_cohort_pick", campaign_id=b["bot_id"]),
        )
    kb.button(text="◀️ Назад", callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🎯 <b>Когортное таргетирование</b>\n\nВыберите бот:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(DmCb.filter(F.action == "target_cohort_pick"))
async def cb_dm_target_cohort_pick(
    callback: CallbackQuery,
    callback_data: DmCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Step 2: pick cohort type (hot/warm/cold/lost)."""
    await callback.answer()
    bot_id = callback_data.campaign_id
    await state.update_data(dm_target_type="cohort", dm_target_id=bot_id)

    # Count each cohort size
    try:
        from database import db as _db
        cohort_stats = await _db.get_user_cohorts(pool, bot_id)
    except Exception:
        log_exc_swallow(log, "Ошибка получения статистики когорт для DM-кампании")
        cohort_stats = {"hot": 0, "warm": 0, "cold": 0, "lost": 0}

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"🔥 Hot (24ч): {cohort_stats.get('hot', 0)} чел.",
        callback_data=DmCb(action="target_cohort_set", campaign_id=0),
    )
    kb.button(
        text=f"🟡 Warm (7д): {cohort_stats.get('warm', 0)} чел.",
        callback_data=DmCb(action="target_cohort_set", campaign_id=1),
    )
    kb.button(
        text=f"🧊 Cold (30д): {cohort_stats.get('cold', 0)} чел.",
        callback_data=DmCb(action="target_cohort_set", campaign_id=2),
    )
    kb.button(
        text=f"💀 Lost (30д+): {cohort_stats.get('lost', 0)} чел.",
        callback_data=DmCb(action="target_cohort_set", campaign_id=3),
    )
    kb.button(text="◀️ Назад", callback_data=DmCb(action="target_cohort_bot"))
    kb.adjust(1)

    bot_row = await pool.fetchrow(
        "SELECT first_name, username FROM managed_bots WHERE bot_id=$1", bot_id
    )
    bot_label = (bot_row.get("first_name") or bot_row.get("username") or str(bot_id)) if bot_row else str(bot_id)

    await callback.message.edit_text(
        f"🎯 <b>Выберите когорту</b> для бота @{html.escape(bot_label)}:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


_COHORT_TYPES = {0: "hot", 1: "warm", 2: "cold", 3: "lost"}
_COHORT_LABELS = {"hot": "🔥 Hot", "warm": "🟡 Warm", "cold": "🧊 Cold", "lost": "💀 Lost"}


@router.callback_query(DmCb.filter(F.action == "target_cohort_set"))
async def cb_dm_target_cohort_set(
    callback: CallbackQuery,
    callback_data: DmCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Step 3: cohort selected — store and go to preview."""
    await callback.answer()
    cohort_type = _COHORT_TYPES.get(callback_data.campaign_id, "warm")
    await state.update_data(dm_cohort_type=cohort_type)
    await _show_dm_preview(callback, state, pool)


# ── Create — Step 3b: CRM ─────────────────────────────────────────────────────

@router.callback_query(DmCb.filter(F.action == "target_crm"))
async def cb_dm_target_crm(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.update_data(dm_target_type="crm", dm_target_id=None)
    await _show_dm_preview(callback, state, pool)


# ── Preview & Confirm ─────────────────────────────────────────────────────────

async def _show_dm_preview(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    sd = await state.get_data()
    name = sd.get("dm_name", "")
    text = sd.get("dm_text", "")
    target_type = sd.get("dm_target_type", "bot_users")
    target_id = sd.get("dm_target_id")

    cohort_type = sd.get("dm_cohort_type", "")

    # Подсчитать аудиторию
    if target_type == "bot_users" and target_id:
        count_row = await pool.fetchrow(
            "SELECT COUNT(DISTINCT chat_id) AS cnt FROM bot_users WHERE bot_id=$1 AND chat_id > 0",
            target_id,
        )
        bot_row = await pool.fetchrow(
            "SELECT first_name, username FROM managed_bots WHERE bot_id=$1",
            target_id,
        )
        bot_label = (bot_row.get("first_name") or bot_row.get("username") or str(target_id)) if bot_row else str(target_id)
        audience_str = f"Все подписчики @{bot_label}: <b>{count_row['cnt']}</b>"
    elif target_type == "cohort" and target_id:
        cohort_sql = {
            "hot":  "last_seen >= now() - INTERVAL '1 day'",
            "warm": "last_seen >= now() - INTERVAL '7 days' AND last_seen < now() - INTERVAL '1 day'",
            "cold": "last_seen >= now() - INTERVAL '30 days' AND last_seen < now() - INTERVAL '7 days'",
            "lost": "last_seen < now() - INTERVAL '30 days'",
        }.get(cohort_type, "last_seen >= now() - INTERVAL '7 days'")
        try:
            cnt = await pool.fetchval(
                f"SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 AND {cohort_sql}",
                target_id,
            ) or 0
        except Exception:
            cnt = 0
        bot_row = await pool.fetchrow(
            "SELECT first_name, username FROM managed_bots WHERE bot_id=$1", target_id
        )
        bot_label = (bot_row.get("first_name") or bot_row.get("username") or str(target_id)) if bot_row else str(target_id)
        cohort_label = _COHORT_LABELS.get(cohort_type, cohort_type)
        audience_str = f"{cohort_label} когорта @{bot_label}: <b>{cnt}</b>"
    else:
        count_row = await pool.fetchrow(
            "SELECT COUNT(DISTINCT tg_user_id) AS cnt FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0",
            callback.from_user.id,
        )
        audience_str = f"CRM-контакты: <b>{count_row['cnt']}</b>"

    from services.dm_engine import expand_spintax
    preview_text = expand_spintax(text)

    lines = [
        "<b>📨 Предпросмотр кампании</b>\n",
        f"Название: <b>{html.escape(name)}</b>",
        f"Аудитория: {audience_str}",
        "\n<b>Пример сообщения:</b>",
        f"<i>{html.escape(preview_text[:300])}</i>",
    ]

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить",  callback_data=DmCb(action="launch"))
    kb.button(text="💾 Сохранить как черновик", callback_data=DmCb(action="save_draft"))
    kb.button(text="❌ Отмена",     callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await _edit(callback, "\n".join(lines), kb.as_markup())


@router.callback_query(DmCb.filter(F.action.in_({"launch", "save_draft"})))
async def cb_dm_launch_or_draft(
    callback: CallbackQuery,
    callback_data: DmCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    name = sd.get("dm_name", "")
    text = sd.get("dm_text", "")
    target_type = sd.get("dm_target_type", "bot_users")
    target_id = sd.get("dm_target_id")
    cohort_type = sd.get("dm_cohort_type", "")
    status = "draft" if callback_data.action == "save_draft" else "running"

    import json as _json
    params_dict = {}
    if cohort_type:
        params_dict["cohort_type"] = cohort_type

    initial_status = "draft"  # всегда создаём как draft, потом меняем
    campaign_id = await pool.fetchval(
        "INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, status, params) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING id",
        callback.from_user.id, name, text, target_type, target_id, initial_status,
        _json.dumps(params_dict) if params_dict else "{}",
    )
    await state.clear()

    if status == "running":
        await pool.execute(
            "UPDATE dm_campaigns SET status='running', started_at=now() WHERE id=$1",
            campaign_id,
        )
        # Запустить асинхронно уже после установки статуса
        _t = asyncio.create_task(_launch_campaign(pool, callback.bot, campaign_id))
        _treg.register(callback.from_user.id, "dm_campaign", f"DM «{name[:30]}»", _t)
        await _edit(
            callback,
            f"🚀 Кампания <b>«{html.escape(name)}»</b> запущена!\n\n"
            f"ID кампании: <code>{campaign_id}</code>\n"
            "Отправка идёт в фоне — вы получите уведомление когда закончится.\n"
            "<i>Для отмены: /tasks</i>",
        )
    else:
        await _edit(
            callback,
            f"💾 Кампания <b>«{html.escape(name)}»</b> сохранена как черновик.\n"
            f"ID: <code>{campaign_id}</code>\n\n"
            "Запустите из меню кампаний когда будете готовы.",
        )


async def _launch_campaign(pool: asyncpg.Pool, bot, campaign_id: int) -> None:
    from services.dm_engine import run_campaign
    try:
        await run_campaign(pool, bot, campaign_id)
    except asyncio.CancelledError:
        await pool.execute(
            "UPDATE dm_campaigns SET status='paused' WHERE id=$1", campaign_id
        )
        raise
    except Exception as e:
        log.exception("dm_engine campaign %d error: %s", campaign_id, e)
        await pool.execute(
            "UPDATE dm_campaigns SET status='failed' WHERE id=$1", campaign_id
        )


# ── Detail ────────────────────────────────────────────────────────────────────

@router.callback_query(DmCb.filter(F.action == "detail"))
async def cb_dm_detail(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    c = await pool.fetchrow(
        "SELECT * FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
        callback_data.campaign_id, callback.from_user.id,
    )
    if not c:
        await callback.answer("Кампания не найдена", show_alert=True)
        return

    status = c["status"]
    icon = _STATUS_EMOJI.get(status, "❓")
    status_label = _STATUS_LABELS.get(status, status)
    total = c["total_targets"] or 0
    sent = c["sent_count"] or 0
    failed = c["fail_count"] or 0
    pct = int(sent * 100 / total) if total > 0 else 0

    started = c["started_at"].strftime("%d.%m %H:%M") if c.get("started_at") else "—"
    finished = c["finished_at"].strftime("%d.%m %H:%M") if c.get("finished_at") else "—"

    lines = [
        f"<b>📨 {html.escape(c['name'])}</b>\n",
        f"Статус: {icon} {status_label}",
        f"Аудитория: <b>{c['target_type']}</b>",
        f"\n📊 Прогресс:",
        f"• Отправлено: <b>{sent}</b> / {total} ({pct}%)",
        f"• Ошибок: <b>{failed}</b>",
        f"\n🕐 Старт: {started}",
        f"🏁 Конец: {finished}",
    ]

    kb = InlineKeyboardBuilder()
    if status == "running":
        kb.button(text="⏸️ Поставить на паузу", callback_data=DmCb(action="pause", campaign_id=c["id"]))
    elif status in ("paused", "draft"):
        kb.button(text="▶️ Запустить/продолжить", callback_data=DmCb(action="resume", campaign_id=c["id"]))
    if status in ("done", "failed"):
        kb.button(text="🗑️ Удалить", callback_data=DmCb(action="delete", campaign_id=c["id"]))
    kb.button(text="◀️ Назад", callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await _edit(callback, "\n".join(lines), kb.as_markup())


@router.callback_query(DmCb.filter(F.action == "pause"))
async def cb_dm_pause(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("⏸️ Поставлена на паузу")
    await pool.execute(
        "UPDATE dm_campaigns SET status='paused' WHERE id=$1 AND owner_id=$2",
        callback_data.campaign_id, callback.from_user.id,
    )
    await cb_dm_detail(callback, callback_data, pool)


@router.callback_query(DmCb.filter(F.action == "resume"))
async def cb_dm_resume(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("▶️ Запущена")
    campaign_id = callback_data.campaign_id
    _t = asyncio.create_task(_launch_campaign(pool, callback.bot, campaign_id))
    _treg.register(callback.from_user.id, "dm_campaign", f"DM campaign #{campaign_id}", _t)
    await pool.execute(
        "UPDATE dm_campaigns SET status='running', started_at=COALESCE(started_at, now()) WHERE id=$1",
        campaign_id,
    )
    await cb_dm_detail(callback, callback_data, pool)


@router.callback_query(DmCb.filter(F.action == "delete"))
async def cb_dm_delete(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("🗑️ Удалено")
    await pool.execute(
        "DELETE FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
        callback_data.campaign_id, callback.from_user.id,
    )
    await cb_dm_menu(callback, pool)

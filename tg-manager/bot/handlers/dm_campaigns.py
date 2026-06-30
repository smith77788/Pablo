"""
DM Campaigns — управление прямыми рассылками подписчикам.

Аудитория: только собственные подписчики ботов и CRM-контакты.
Entry: DmCb(action="menu")
"""

from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import DmCb, BmCb
from bot.states import DmCampaignFSM
from services import intelligence_engine
from services.logger import log_exc_swallow
from bot.utils.subscription import require_plan, locked_text

from bot.utils.event_status import mark_handled_error
from bot.utils.op_helpers import safe_answer

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
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return
        if "there is no text in the message to edit" in err_str:
            try:
                await callback.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
                return
            except Exception:
                pass
        if "message to edit not found" in err_str or "message can't be edited" in err_str:
            await callback.bot.send_message(callback.from_user.id, text, parse_mode="HTML", reply_markup=markup)
        else:
            log.warning("dm_campaigns _edit error: %s", e)


# ── Menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(DmCb.filter(F.action == "menu"))
async def cb_dm_menu(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    # Clear any lingering FSM state (e.g. user pressed "cancel" mid-wizard)
    await state.clear()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await safe_answer(callback)
        await _edit(
            callback,
            locked_text("DM-кампании", "enterprise"),
            subscription_locked_markup("enterprise", back_callback=BmCb(action="comms")),
        )
        return
    await safe_answer(callback)

    try:
        campaigns = await pool.fetch(
            "SELECT id, name, status, sent_count, fail_count, total_targets, created_at "
            "FROM dm_campaigns WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 10",
            callback.from_user.id,
        )
    except Exception:
        campaigns = []

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новая кампания", callback_data=DmCb(action="new"))

    # Build campaign cards with status + counts + quick action buttons
    campaign_lines = []
    for c in campaigns:
        icon = _STATUS_EMOJI.get(c["status"], "❓")
        status_label = _STATUS_LABELS.get(c["status"], c["status"])
        total = c["total_targets"] or 0
        sent = c["sent_count"] or 0
        fail = c["fail_count"] or 0
        pct = int(sent * 100 / total) if total > 0 else 0
        name_short = c["name"][:28]
        campaign_lines.append(
            f"{icon} <b>{name_short}</b> — {status_label}\n"
            f"   📤 {sent}/{total} ({pct}%) | ❌ {fail}"
        )
        # Row: detail + quick action
        kb.button(
            text=f"{icon} {c['name'][:20]}",
            callback_data=DmCb(action="detail", campaign_id=c["id"]),
        )
        if c["status"] == "running":
            kb.button(text="⏸", callback_data=DmCb(action="pause", campaign_id=c["id"]))
        elif c["status"] in ("paused", "draft"):
            kb.button(
                text="▶️", callback_data=DmCb(action="resume", campaign_id=c["id"])
            )
        else:
            kb.button(
                text="🗑", callback_data=DmCb(action="delete", campaign_id=c["id"])
            )

    kb.button(text="◀️ Назад", callback_data=BmCb(action="comms"))
    # Adjust: [new], then [detail, action] pairs, then [back]
    if campaigns:
        kb.adjust(1, *([2] * len(campaigns)), 1)
    else:
        kb.adjust(1)

    running_count = sum(1 for c in campaigns if c["status"] == "running")
    cards_text = ("\n\n" + "\n\n".join(campaign_lines)) if campaign_lines else ""
    if not campaigns:
        empty_hint = (
            "\n\n💡 У вас пока нет кампаний.\n"
            "Нажмите <b>➕ Новая кампания</b>, чтобы создать первую рассылку!"
        )
    else:
        empty_hint = ""
    text = (
        "<b>📨 DM-кампании</b>\n\n"
        "Отправляйте персонализированные сообщения своим подписчикам.\n\n"
        "📌 <i>Аудитория: подписчики ваших ботов и CRM-контакты.</i>\n"
        f"Кампаний: <b>{len(campaigns)}</b>"
        + (f" | Активных: <b>{running_count}</b>" if running_count else "")
        + empty_hint
        + cards_text
    )
    await _edit(callback, text, kb.as_markup())


# ── Create — Step 1: name ─────────────────────────────────────────────────────


@router.callback_query(DmCb.filter(F.action == "new"))
async def cb_dm_new(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await safe_answer(callback)
        await callback.message.edit_text(
            locked_text("DM-кампании", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise", back_callback=BmCb(action="comms")),
        )
        return
    await safe_answer(callback)
    await state.set_state(DmCampaignFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
    await callback.message.answer(
        "📨 <b>Новая DM-кампания</b>\n\nВведите <b>название</b> кампании:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(DmCampaignFSM.waiting_name)
async def fsm_dm_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        _cancel_kb = InlineKeyboardBuilder()
        _cancel_kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
        await message.answer("⚠️ Введите название:", reply_markup=_cancel_kb.as_markup())
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
        _cancel_kb = InlineKeyboardBuilder()
        _cancel_kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
        await message.answer(
            "⚠️ Текст не может быть пустым:", reply_markup=_cancel_kb.as_markup()
        )
        return
    await state.update_data(dm_text=text.strip())
    await state.set_state(DmCampaignFSM.choosing_target)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🤖 Подписчики одного бота",
        callback_data=DmCb(action="target_type", campaign_id=0),
    )
    kb.button(
        text="🤖 Все подписчики (все боты)",
        callback_data=DmCb(action="target_all_bots"),
    )
    kb.button(
        text="🎯 По когорте (активность)",
        callback_data=DmCb(action="target_cohort_bot"),
    )
    kb.button(text="👥 CRM-контакты", callback_data=DmCb(action="target_crm"))
    kb.button(text="🔍 Спарсенная аудитория", callback_data=DmCb(action="target_parsed"))
    kb.button(text="📋 Импорт списка (@username / ID)", callback_data=DmCb(action="target_import"))
    kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "👥 <b>Выберите аудиторию</b>:\n\n"
        "🤖 <b>Подписчики одного бота</b> — выбрать конкретный бот\n"
        "🤖 <b>Все подписчики (все боты)</b> — агрегат по всем вашим ботам\n\n"
        "🎯 <b>По когорте</b> — сегмент по активности:\n"
        "  • 🔥 Hot — активны за 24ч\n"
        "  • 🟡 Warm — 1-7 дней назад\n"
        "  • 🧊 Cold — 7-30 дней назад\n"
        "  • 💀 Lost — неактивны 30+ дней\n\n"
        "👥 <b>CRM-контакты</b> — ваша CRM-база\n"
        "🔍 <b>Спарсенная аудитория</b> — результаты парсера\n"
        "📋 <b>Импорт списка</b> — вставить @usernames или Telegram ID",
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
    await safe_answer(callback)
    await state.update_data(dm_target_type="bot_users")
    await state.set_state(DmCampaignFSM.choosing_bot)

    try:
        bots = await pool.fetch(
            "SELECT bot_id, first_name, username FROM managed_bots "
            "WHERE added_by=$1 AND is_active=true ORDER BY first_name LIMIT 20",
            callback.from_user.id,
        )
    except Exception:
        bots = []

    kb = InlineKeyboardBuilder()
    for b in bots:
        label = b.get("first_name") or b.get("username") or f"bot_{b['bot_id']}"
        kb.button(
            text=f"🤖 {label[:28]}",
            callback_data=DmCb(action="target_bot_id", campaign_id=b["bot_id"]),
        )
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
    await safe_answer(callback)
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
    await safe_answer(callback)
    try:
        bots = await pool.fetch(
            "SELECT bot_id, first_name, username FROM managed_bots "
            "WHERE added_by=$1 AND is_active=true ORDER BY first_name LIMIT 20",
            callback.from_user.id,
        )
    except Exception:
        bots = []
    if not bots:
        _no_bots_kb = InlineKeyboardBuilder()
        _no_bots_kb.button(text="◀️ Назад", callback_data=DmCb(action="menu"))
        await callback.message.edit_text(
            "⚠️ У вас нет активных ботов.",
            parse_mode="HTML",
            reply_markup=_no_bots_kb.as_markup(),
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
    await safe_answer(callback)
    bot_id = callback_data.campaign_id
    await state.update_data(dm_target_type="cohort", dm_target_id=bot_id)

    # Count each cohort size
    try:
        from database import db as _db

        cohort_stats = await _db.get_activity_segments(pool, bot_id)
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

    try:
        bot_row = await pool.fetchrow(
            "SELECT first_name, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            bot_id,
            callback.from_user.id,
        )
    except Exception:
        bot_row = None
    bot_label = (
        (bot_row.get("first_name") or bot_row.get("username") or str(bot_id))
        if bot_row
        else str(bot_id)
    )

    await callback.message.edit_text(
        f"🎯 <b>Выберите когорту</b> для бота @{html.escape(bot_label)}:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


_COHORT_TYPES = {0: "hot", 1: "warm", 2: "cold", 3: "lost"}
_COHORT_LABELS = {
    "hot": "🔥 Hot",
    "warm": "🟡 Warm",
    "cold": "🧊 Cold",
    "lost": "💀 Lost",
}


@router.callback_query(DmCb.filter(F.action == "target_cohort_set"))
async def cb_dm_target_cohort_set(
    callback: CallbackQuery,
    callback_data: DmCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Step 3: cohort selected — store and go to preview."""
    await safe_answer(callback)
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
    await safe_answer(callback)
    await state.update_data(dm_target_type="crm", dm_target_id=None)
    await _show_dm_preview(callback, state, pool)


# ── Create — Step 3d: Parsed Audience ────────────────────────────────────────


@router.callback_query(DmCb.filter(F.action == "target_parsed"))
async def cb_dm_target_parsed(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Show list of parser runs so user can pick one (or use all)."""
    await safe_answer(callback)

    try:
        runs = await pool.fetch(
            """SELECT id, source_ref, parse_type, total_found, started_at
               FROM parser_runs
               WHERE owner_id=$1 AND status='done' AND total_found > 0
               ORDER BY started_at DESC LIMIT 10""",
            callback.from_user.id,
        )
    except Exception:
        runs = []

    try:
        total_all = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1",
                callback.from_user.id,
            )
            or 0
        )
    except Exception:
        total_all = 0

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"🗂 Вся аудитория ({total_all:,} чел.)",
        callback_data=DmCb(action="target_parsed_pick", campaign_id=0),
    )
    for r in runs:
        started = r["started_at"].strftime("%d.%m %H:%M") if r["started_at"] else "—"
        label = f"{r['source_ref'][:18]} [{r['parse_type']}] {r['total_found']:,} чел. {started}"
        kb.button(
            text=label,
            callback_data=DmCb(action="target_parsed_pick", campaign_id=r["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=DmCb(action="menu"))
    kb.adjust(1)

    if not runs and total_all == 0:
        await _edit(
            callback,
            "⚠️ <b>Нет спарсенной аудитории</b>\n\n"
            "Сначала запустите парсер: Аудитория → Парсер аудитории",
            kb.as_markup(),
        )
        return

    await _edit(
        callback,
        "🔍 <b>Спарсенная аудитория</b>\n\n"
        "Выберите конкретный запуск парсера или всю аудиторию:",
        kb.as_markup(),
    )


@router.callback_query(DmCb.filter(F.action == "target_parsed_pick"))
async def cb_dm_target_parsed_pick(
    callback: CallbackQuery,
    callback_data: DmCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Store parsed_audience target (run_id=0 means all) and go to preview."""
    await safe_answer(callback)
    run_id = callback_data.campaign_id  # 0 = all, >0 = specific run
    await state.update_data(dm_target_type="parsed_audience", dm_target_id=run_id)
    await _show_dm_preview(callback, state, pool)


# ── Create — Step 3e: All bots aggregate ─────────────────────────────────────


@router.callback_query(DmCb.filter(F.action == "target_all_bots"))
async def cb_dm_target_all_bots(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Все подписчики всех ботов пользователя — агрегат."""
    await safe_answer(callback)
    try:
        cnt = await pool.fetchval(
            """SELECT COUNT(DISTINCT bu.user_id)
               FROM bot_users bu
               JOIN managed_bots mb ON mb.bot_id = bu.bot_id
               WHERE mb.added_by=$1 AND bu.user_id > 0""",
            callback.from_user.id,
        ) or 0
    except Exception:
        cnt = 0
    if not cnt:
        _no_kb = InlineKeyboardBuilder()
        _no_kb.button(text="◀️ Назад", callback_data=DmCb(action="menu"))
        await _edit(
            callback,
            "⚠️ <b>Нет подписчиков ни в одном боте</b>\n\n"
            "Добавьте подписчиков к вашим ботам, затем попробуйте снова.",
            _no_kb.as_markup(),
        )
        return
    await state.update_data(dm_target_type="all_bots", dm_target_id=None)
    await _show_dm_preview(callback, state, pool)


# ── Create — Step 3f: Import list ────────────────────────────────────────────


@router.callback_query(DmCb.filter(F.action == "target_import"))
async def cb_dm_target_import(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Импорт списка получателей (@username или Telegram ID)."""
    await safe_answer(callback)
    await state.set_state(DmCampaignFSM.waiting_import_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await _edit(
        callback,
        "📋 <b>Импорт списка получателей</b>\n\n"
        "Введите получателей — каждый с новой строки (или через запятую):\n\n"
        "• <code>@username</code> — по юзернейму\n"
        "• <code>123456789</code> — по Telegram ID\n\n"
        "<b>Пример:</b>\n"
        "<code>@ivan_petrov\n"
        "@maria_smirnova\n"
        "987654321\n"
        "@user123</code>\n\n"
        "<i>Максимум 5 000 получателей за раз.</i>",
        kb.as_markup(),
    )


@router.message(DmCampaignFSM.waiting_import_text)
async def fsm_dm_import_text(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    import re as _re

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("⚠️ Список пустой. Введите хотя бы одного получателя:")
        return

    items: list[dict] = []
    errors = 0
    for part in _re.split(r"[\n,;]+", raw):
        part = part.strip()
        if not part:
            continue
        if part.startswith("@"):
            uname = part.lstrip("@").strip()
            if uname:
                items.append({"user_id": 0, "username": uname})
        elif part.isdigit() and len(part) >= 4:
            items.append({"user_id": int(part), "username": None})
        elif part and not part.isspace():
            # попробовать как username без @
            clean = part.strip()
            if clean.isalnum() or "_" in clean:
                items.append({"user_id": 0, "username": clean})
            else:
                errors += 1

    if not items:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
        await message.answer(
            "⚠️ Не удалось распознать ни одного получателя.\n"
            "Используйте формат <code>@username</code> или числовой Telegram ID.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    truncated = len(items) > 5000
    if truncated:
        items = items[:5000]

    await state.update_data(
        dm_target_type="import_list",
        dm_target_id=None,
        dm_import_list=items,
    )

    trunc_note = "\n⚠️ Список обрезан до 5 000 получателей." if truncated else ""
    err_note = f"\n⚠️ {errors} строк не распознано — пропущены." if errors else ""
    preview_lines = []
    for it in items[:5]:
        if it.get("username"):
            preview_lines.append(f"• @{it['username']}")
        else:
            preview_lines.append(f"• {it['user_id']}")
    preview_str = "\n".join(preview_lines)
    if len(items) > 5:
        preview_str += f"\n<i>... и ещё {len(items) - 5}</i>"

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=DmCb(action="import_confirm"))
    kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"📋 <b>Список импортирован: {len(items)} получателей</b>\n\n"
        f"{preview_str}"
        f"{trunc_note}{err_note}\n\n"
        "Подтвердить?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(DmCb.filter(F.action == "import_confirm"))
async def cb_dm_import_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
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
    recipients_count = 0
    if target_type == "bot_users" and target_id:
        try:
            count_row = await pool.fetchrow(
                "SELECT COUNT(DISTINCT user_id) AS cnt FROM bot_users WHERE bot_id=$1 AND user_id > 0",
                target_id,
            )
        except Exception:
            count_row = None
        try:
            bot_row = await pool.fetchrow(
                "SELECT first_name, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                target_id,
                callback.from_user.id,
            )
        except Exception:
            bot_row = None
        bot_label = (
            (bot_row.get("first_name") or bot_row.get("username") or str(target_id))
            if bot_row
            else str(target_id)
        )
        recipients_count = int((count_row["cnt"] if count_row else None) or 0)
        audience_str = f"Все подписчики @{bot_label}: <b>{recipients_count}</b>"
    elif target_type == "cohort" and target_id:
        cohort_sql = {
            "hot": "last_seen >= now() - INTERVAL '1 day'",
            "warm": "last_seen >= now() - INTERVAL '7 days' AND last_seen < now() - INTERVAL '1 day'",
            "cold": "last_seen >= now() - INTERVAL '30 days' AND last_seen < now() - INTERVAL '7 days'",
            "lost": "last_seen < now() - INTERVAL '30 days'",
        }.get(cohort_type, "last_seen >= now() - INTERVAL '7 days'")
        try:
            cnt = (
                await pool.fetchval(
                    f"SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 AND {cohort_sql}",
                    target_id,
                )
                or 0
            )
        except Exception:
            log_exc_swallow(log, "Ошибка подсчёта размера когорты для DM-кампании")
            cnt = 0
        try:
            bot_row = await pool.fetchrow(
                "SELECT first_name, username FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
                target_id,
                callback.from_user.id,
            )
        except Exception:
            bot_row = None
        bot_label = (
            (bot_row.get("first_name") or bot_row.get("username") or str(target_id))
            if bot_row
            else str(target_id)
        )
        cohort_label = _COHORT_LABELS.get(cohort_type, cohort_type)
        recipients_count = int(cnt)
        audience_str = f"{cohort_label} когорта @{bot_label}: <b>{cnt}</b>"
    elif target_type == "parsed_audience":
        try:
            if target_id:
                cnt = (
                    await pool.fetchval(
                        "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2",
                        callback.from_user.id,
                        target_id,
                    )
                    or 0
                )
                run_row = await pool.fetchrow(
                    "SELECT source_ref, parse_type FROM parser_runs WHERE id=$1",
                    target_id,
                )
                src = (run_row["source_ref"] if run_row else str(target_id))
                parse_label = f"{html.escape(src[:30])}"
            else:
                cnt = (
                    await pool.fetchval(
                        "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1",
                        callback.from_user.id,
                    )
                    or 0
                )
                parse_label = "вся аудитория"
        except Exception:
            log_exc_swallow(log, "Ошибка подсчёта спарсенной аудитории для DM-кампании")
            cnt = 0
            parse_label = "спарсенная"
        recipients_count = int(cnt)
        audience_str = f"🔍 Спарсенная ({parse_label}): <b>{cnt}</b>"
    elif target_type == "all_bots":
        try:
            cnt = (
                await pool.fetchval(
                    """SELECT COUNT(DISTINCT bu.user_id)
                       FROM bot_users bu
                       JOIN managed_bots mb ON mb.bot_id = bu.bot_id
                       WHERE mb.added_by=$1 AND bu.user_id > 0""",
                    callback.from_user.id,
                )
                or 0
            )
        except Exception:
            log_exc_swallow(log, "Ошибка подсчёта аудитории all_bots для DM-кампании")
            cnt = 0
        recipients_count = int(cnt)
        audience_str = f"🤖 Все подписчики (все боты): <b>{cnt}</b>"
    elif target_type == "import_list":
        import_list = sd.get("dm_import_list") or []
        cnt = len(import_list)
        recipients_count = cnt
        audience_str = f"📋 Импортированный список: <b>{cnt}</b>"
    else:
        try:
            count_row = await pool.fetchrow(
                "SELECT COUNT(DISTINCT tg_user_id) AS cnt FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0",
                callback.from_user.id,
            )
        except Exception:
            count_row = None
        recipients_count = int((count_row["cnt"] if count_row else None) or 0)
        audience_str = f"CRM-контакты: <b>{recipients_count}</b>"

    from services.dm_engine import expand_spintax

    preview_text = expand_spintax(text)

    # Intelligence block
    try:
        intel = await intelligence_engine.get_pre_launch_intelligence(
            pool,
            callback.from_user.id,
            "dm_campaign",
            recipients_count,
        )
        intel_text = "\n\n" + intelligence_engine.format_pre_launch_block(intel)
    except Exception:
        intel_text = ""

    lines = [
        "<b>📨 Предпросмотр кампании</b>\n",
        f"Название: <b>{html.escape(name)}</b>",
        f"Аудитория: {audience_str}",
        "\n<b>Пример сообщения:</b>",
        f"<i>{html.escape(preview_text[:300])}</i>",
    ]
    if intel_text:
        lines.append(intel_text)

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=DmCb(action="launch"))
    kb.button(text="💾 Сохранить как черновик", callback_data=DmCb(action="save_draft"))
    kb.button(text="❌ Отмена", callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await _edit(callback, "\n".join(lines), kb.as_markup())


@router.callback_query(DmCb.filter(F.action.in_({"launch", "save_draft"})))
async def cb_dm_launch_or_draft(
    callback: CallbackQuery,
    callback_data: DmCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await safe_answer(callback)
        await callback.message.edit_text(
            locked_text("DM-кампании", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise", back_callback=BmCb(action="comms")),
        )
        return
    await safe_answer(callback)
    sd = await state.get_data()
    name = sd.get("dm_name", "")
    text = sd.get("dm_text", "")
    target_type = sd.get("dm_target_type", "bot_users")
    target_id = sd.get("dm_target_id")
    cohort_type = sd.get("dm_cohort_type", "")
    status = "draft" if callback_data.action == "save_draft" else "running"

    # Block launch if audience is empty (allow save_draft regardless)
    if status == "running":
        try:
            audience_cnt = 0
            if target_type == "bot_users" and target_id:
                audience_cnt = (
                    await pool.fetchval(
                        "SELECT COUNT(DISTINCT user_id) FROM bot_users WHERE bot_id=$1 AND user_id > 0",
                        target_id,
                    )
                    or 0
                )
            elif target_type == "cohort" and target_id:
                cohort_sql = {
                    "hot": "last_seen >= now() - INTERVAL '1 day'",
                    "warm": "last_seen >= now() - INTERVAL '7 days' AND last_seen < now() - INTERVAL '1 day'",
                    "cold": "last_seen >= now() - INTERVAL '30 days' AND last_seen < now() - INTERVAL '7 days'",
                    "lost": "last_seen < now() - INTERVAL '30 days'",
                }.get(cohort_type, "last_seen >= now() - INTERVAL '7 days'")
                audience_cnt = (
                    await pool.fetchval(
                        f"SELECT COUNT(*) FROM user_activity WHERE bot_id=$1 AND {cohort_sql}",
                        target_id,
                    )
                    or 0
                )
            elif target_type == "crm":
                audience_cnt = (
                    await pool.fetchval(
                        "SELECT COUNT(DISTINCT tg_user_id) FROM crm_contacts WHERE owner_id=$1 AND tg_user_id > 0",
                        callback.from_user.id,
                    )
                    or 0
                )
            elif target_type == "parsed_audience":
                if target_id:
                    audience_cnt = (
                        await pool.fetchval(
                            "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1 AND parse_run_id=$2",
                            callback.from_user.id,
                            target_id,
                        )
                        or 0
                    )
                else:
                    audience_cnt = (
                        await pool.fetchval(
                            "SELECT COUNT(DISTINCT tg_user_id) FROM parsed_audiences WHERE owner_id=$1",
                            callback.from_user.id,
                        )
                        or 0
                    )
            elif target_type == "all_bots":
                audience_cnt = (
                    await pool.fetchval(
                        """SELECT COUNT(DISTINCT bu.user_id)
                           FROM bot_users bu
                           JOIN managed_bots mb ON mb.bot_id = bu.bot_id
                           WHERE mb.added_by=$1 AND bu.user_id > 0""",
                        callback.from_user.id,
                    )
                    or 0
                )
            elif target_type == "import_list":
                audience_cnt = len(sd.get("dm_import_list") or [])
            if audience_cnt == 0:
                _empty_kb = InlineKeyboardBuilder()
                _empty_kb.button(
                    text="💾 Сохранить как черновик",
                    callback_data=DmCb(action="save_draft"),
                )
                _empty_kb.button(text="◀️ Назад", callback_data=DmCb(action="menu"))
                _empty_kb.adjust(1)
                await _edit(
                    callback,
                    "⚠️ <b>Аудитория пуста — кампания не запущена</b>\n\n"
                    "В выбранной аудитории нет получателей.\n"
                    "Сохраните как черновик или выберите другую аудиторию.",
                    _empty_kb.as_markup(),
                )
                return
        except Exception:
            log_exc_swallow(log, "Ошибка проверки аудитории перед запуском DM-кампании")

    import json as _json

    params_dict = {}
    if cohort_type:
        params_dict["cohort_type"] = cohort_type
    if target_type == "import_list":
        params_dict["import_list"] = sd.get("dm_import_list") or []

    initial_status = "draft"  # всегда создаём как draft, потом меняем
    try:
        campaign_id = await pool.fetchval(
            "INSERT INTO dm_campaigns(owner_id, name, text_template, target_type, target_id, status, params) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) RETURNING id",
            callback.from_user.id,
            name,
            text,
            target_type,
            target_id,
            initial_status,
            _json.dumps(params_dict, ensure_ascii=False) if params_dict else "{}",
        )
    except Exception as exc:
        mark_handled_error(f"dm_launch insert: {exc}")
        await _edit(
            callback,
            f"❌ Ошибка создания кампании: <code>{html.escape(str(exc)[:200])}</code>",
            InlineKeyboardBuilder()
            .button(text="◀️ Назад", callback_data=DmCb(action="menu"))
            .as_markup(),
        )
        return
    await state.clear()

    if status == "running":
        from services import infra_orchestrator, operation_bus

        ready, reason = await infra_orchestrator.is_ready_for_op(
            pool, callback.from_user.id
        )
        if not ready:
            try:
                await pool.execute("DELETE FROM dm_campaigns WHERE id=$1", campaign_id)
            except Exception:
                pass
            await _edit(
                callback,
                f"⚠️ {reason}\n\nКампания не запущена.",
                InlineKeyboardBuilder()
                .button(text="◀️ Назад", callback_data=DmCb(action="menu"))
                .as_markup(),
            )
            return
        # Submit to operation_queue so op_worker handles execution with proper
        # retry logic, progress tracking, and cancellation support.
        try:
            op_id = await operation_bus.submit(
                pool,
                callback.from_user.id,
                "dm_campaign",
                {"campaign_id": campaign_id},
            )
            await pool.execute(
                "UPDATE dm_campaigns SET status='running' WHERE id=$1",
                campaign_id,
            )
        except Exception as exc:
            await _edit(
                callback,
                f"❌ Ошибка постановки кампании в очередь: <code>{html.escape(str(exc)[:200])}</code>",
                InlineKeyboardBuilder()
                .button(text="◀️ Назад", callback_data=DmCb(action="menu"))
                .as_markup(),
            )
            return
        _launch_kb = InlineKeyboardBuilder()
        _launch_kb.button(
            text="📋 Детали кампании",
            callback_data=DmCb(action="detail", campaign_id=campaign_id),
        )
        _launch_kb.button(text="◀️ К кампаниям", callback_data=DmCb(action="menu"))
        _launch_kb.adjust(1)
        await _edit(
            callback,
            f"🚀 Кампания <b>«{html.escape(name)}»</b> поставлена в очередь!\n\n"
            f"ID кампании: <code>{campaign_id}</code>\n"
            f"ID операции: <code>#{op_id}</code>\n"
            "Отправка идёт в фоне — вы получите уведомление когда закончится.\n"
            "<i>Управление: /ops → 📋 Очередь</i>",
            _launch_kb.as_markup(),
        )
    else:
        _draft_kb = InlineKeyboardBuilder()
        _draft_kb.button(
            text="📋 Детали кампании",
            callback_data=DmCb(action="detail", campaign_id=campaign_id),
        )
        _draft_kb.button(text="◀️ К кампаниям", callback_data=DmCb(action="menu"))
        _draft_kb.adjust(1)
        await _edit(
            callback,
            f"💾 Кампания <b>«{html.escape(name)}»</b> сохранена как черновик.\n"
            f"ID: <code>{campaign_id}</code>\n\n"
            "Запустите из меню кампаний когда будете готовы.",
            _draft_kb.as_markup(),
        )


# ── Detail ────────────────────────────────────────────────────────────────────


@router.callback_query(DmCb.filter(F.action == "detail"))
async def cb_dm_detail(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
) -> None:
    try:
        c = await pool.fetchrow(
            "SELECT * FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
            callback_data.campaign_id,
            callback.from_user.id,
        )
    except Exception:
        c = None
    if not c:
        await callback.answer("Кампания не найдена", show_alert=True)
        return
    await safe_answer(callback)

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
        "\n📊 Прогресс:",
        f"• Отправлено: <b>{sent}</b> / {total} ({pct}%)",
        f"• Ошибок: <b>{failed}</b>",
        f"\n🕐 Старт: {started}",
        f"🏁 Конец: {finished}",
    ]

    # Progress bar for running/paused campaigns
    if total > 0 and status in ("running", "paused"):
        bar_len = 10
        filled = int(sent * bar_len / total)
        bar = "█" * filled + "░" * (bar_len - filled)
        lines.append(f"\n[{bar}] {pct}%")

    kb = InlineKeyboardBuilder()
    if status == "running":
        kb.button(
            text="🔄 Обновить", callback_data=DmCb(action="detail", campaign_id=c["id"])
        )
        kb.button(
            text="⏸️ Поставить на паузу",
            callback_data=DmCb(action="pause", campaign_id=c["id"]),
        )
    elif status in ("paused", "draft"):
        kb.button(
            text="▶️ Запустить/продолжить",
            callback_data=DmCb(action="resume", campaign_id=c["id"]),
        )
    if status in ("done", "failed"):
        kb.button(
            text="🗑️ Удалить", callback_data=DmCb(action="delete", campaign_id=c["id"])
        )
    kb.button(text="◀️ Назад", callback_data=DmCb(action="menu"))
    kb.adjust(1)
    await _edit(callback, "\n".join(lines), kb.as_markup())


@router.callback_query(DmCb.filter(F.action == "pause"))
async def cb_dm_pause(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
) -> None:
    campaign_id = callback_data.campaign_id
    # Cancel running operation_queue entry for this campaign (if any).
    # op_worker checks for cancellation every iteration and will stop the dm_engine loop.
    try:
        await pool.execute(
            """UPDATE operation_queue
               SET status='cancelled', finished_at=NOW()
               WHERE owner_id=$1 AND op_type='dm_campaign'
                 AND params->>'campaign_id' = $2::text
                 AND status IN ('pending','running')""",
            callback.from_user.id,
            str(campaign_id),
        )
    except Exception as e:
        log.warning("dm_pause: operation_queue cancel failed campaign_id=%d: %s", campaign_id, e)
    # Also update dm_campaigns status so dm_engine loop exits on next poll
    try:
        await pool.execute(
            "UPDATE dm_campaigns SET status='paused' WHERE id=$1 AND owner_id=$2",
            campaign_id,
            callback.from_user.id,
        )
    except Exception as e:
        log.error("dm_pause: failed to set campaign status=paused campaign_id=%d: %s", campaign_id, e)
    await cb_dm_detail(callback, callback_data, pool)


@router.callback_query(DmCb.filter(F.action == "resume"))
async def cb_dm_resume(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await safe_answer(callback)
        await callback.message.edit_text(
            locked_text("DM-кампании", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise", back_callback=BmCb(action="comms")),
        )
        return
    campaign_id = callback_data.campaign_id
    # Submit to operation_queue — op_worker will call dm_engine.run_campaign()
    # which handles status→running and sends DMs with flood/skip handling.
    try:
        from services import operation_bus

        await operation_bus.submit(
            pool,
            callback.from_user.id,
            "dm_campaign",
            {"campaign_id": campaign_id},
        )
    except Exception as exc:
        await safe_answer(callback)
        import html as _h
        await callback.message.answer(
            f"⚠️ Ошибка постановки в очередь: {_h.escape(str(exc)[:200])}",
            parse_mode="HTML",
        )
        return
    # cb_dm_detail calls callback.answer() itself
    await cb_dm_detail(callback, callback_data, pool)


@router.callback_query(DmCb.filter(F.action == "delete"))
async def cb_dm_delete(
    callback: CallbackQuery,
    callback_data: DmCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer("🗑️ Удалено")
    try:
        await pool.execute(
            "DELETE FROM dm_campaigns WHERE id=$1 AND owner_id=$2",
            callback_data.campaign_id,
            callback.from_user.id,
        )
    except Exception:
        pass
    await cb_dm_menu(callback, pool, state)

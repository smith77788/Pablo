"""Add, list, select, delete managed bots."""

import html
import re
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg
from bot.callbacks import BotCb, SubCb
from bot.keyboards import bots_list, bot_menu, confirm_delete, main_menu
from bot.states import AddBot
from bot.utils.subscription import get_bot_limit
from database import db
from services import bot_api

_TOKEN_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{35,}$")

router = Router()


def _bot_label(row: asyncpg.Record) -> str:
    return f"@{row['username']}" if row["username"] else row["first_name"]


# ── Main menu (inline) ───────────────────────────────────────────────────


@router.callback_query(BotCb.filter(F.action == "main"))
async def cb_main_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from bot.utils.subscription import is_platform_admin

    admin = is_platform_admin(callback.from_user.id)
    bots = await db.get_bots(pool, callback.from_user.id)
    bot_count = len(bots)
    if not bot_count:
        await callback.message.edit_text(
            "👋 <b>BotMother OS</b>\n\nУ вас пока нет добавленных ботов.\nНажмите ➕ Добавить бота.",
            parse_mode="HTML",
            reply_markup=main_menu(is_admin=admin),
        )
    else:
        await callback.message.edit_text(
            f"👋 <b>BotMother OS</b>\n\n🤖 Ботов: <b>{bot_count}</b>\n\nВыберите раздел:",
            parse_mode="HTML",
            reply_markup=main_menu(is_admin=admin),
        )


# ── List ──────────────────────────────────────────────────────────────────


@router.callback_query(BotCb.filter(F.action == "list"))
async def cb_list(
    callback: CallbackQuery, callback_data: BotCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    bots = await db.get_bots(pool, callback.from_user.id)
    hint = (
        "\n\n📌 <b>Что это?</b>\n"
        "Здесь все ваши Telegram-боты. Выберите бота для управления его функциями.\n\n"
        "💡 <b>Как использовать:</b>\n"
        "• Нажмите на бота, чтобы открыть его меню\n"
        "• ➕ Добавить бота — подключите нового через токен BotFather\n"
        "• Каждый бот управляется независимо"
    )
    if not bots:
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        empty_kb = InlineKeyboardBuilder()
        empty_kb.button(text="➕ Добавить бота", callback_data=BotCb(action="add"))
        empty_kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
        empty_kb.adjust(1)
        await callback.message.edit_text(
            "🤖 <b>Мои боты</b>\n\n"
            "У вас пока нет добавленных ботов.\n\n"
            "💡 Нажмите <b>➕ Добавить бота</b> и вставьте токен от @BotFather." + hint,
            parse_mode="HTML",
            reply_markup=empty_kb.as_markup(),
        )
    else:
        await callback.message.edit_text(
            f"🤖 <b>Ваши боты</b> — {len(bots)} шт." + hint,
            parse_mode="HTML",
            reply_markup=bots_list(bots, callback_data.page),
        )


# ── Add — step 1: ask token ───────────────────────────────────────────────


@router.callback_query(BotCb.filter(F.action == "add"))
async def cb_add(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    from bot.utils.subscription import get_plan, get_effective_bot_count

    current_plan = await get_plan(pool, callback.from_user.id)
    limit = await get_bot_limit(pool, callback.from_user.id)
    effective_count = await get_effective_bot_count(pool, callback.from_user.id)
    current_bots = await db.get_bots(pool, callback.from_user.id)
    if effective_count >= limit:
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        kb = InlineKeyboardBuilder()

        if current_plan == "free":
            kb.button(
                text="💳 Оформить подписку",
                callback_data=SubCb(action="choose_plan", plan="paid"),
            )
            kb.button(text="📋 Подписка", callback_data=SubCb(action="menu"))
            upgrade_text = (
                f"⛔️ <b>Достигнут лимит ботов</b>\n\n"
                f"На бесплатном тарифе доступен <b>{limit}</b> бот.\n"
                f"У вас добавлено: <b>{len(current_bots)}</b>\n\n"
                "💎 <b>ПОДПИСКА</b> — без ограничений\n"
                "<i>∞ ботов и каналов, CRM, воронки, аккаунты, AI, рассылки, аналитика</i>\n\n"
                "Оформите подписку, чтобы продолжить добавлять ботов."
            )
        else:
            kb.button(text="📋 Подписка", callback_data=SubCb(action="menu"))
            upgrade_text = (
                f"⛔️ <b>Достигнут лимит ботов</b>\n\n"
                f"На вашем тарифе можно добавить максимум <b>{limit}</b> бот(ов).\n"
                f"У вас уже добавлено: <b>{len(current_bots)}</b>"
            )

        kb.adjust(1)
        await callback.message.edit_text(
            upgrade_text,
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    await state.set_state(AddBot.waiting_token)
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    _cancel_kb = InlineKeyboardBuilder()
    _cancel_kb.button(text="❌ Отмена", callback_data=BotCb(action="list", page=0))
    await callback.message.edit_text(
        "🔑 <b>Добавление бота</b>\n\n"
        "Отправьте токен бота (получить у @BotFather):\n\n"
        "<code>123456789:AAF...</code>\n\n"
        f"<i>Добавлено {len(current_bots)} из {limit} доступных ботов</i>",
        parse_mode="HTML",
        reply_markup=_cancel_kb.as_markup(),
    )


# ── Add — step 2: receive token ───────────────────────────────────────────


@router.message(AddBot.waiting_token, F.text)
async def msg_token(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    token = message.text.strip()

    # Validate token format before hitting Telegram API
    if not _TOKEN_RE.match(token):
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        _fmt_kb = InlineKeyboardBuilder()
        _fmt_kb.button(text="❌ Отмена", callback_data=BotCb(action="list", page=0))
        await message.answer(
            "❌ <b>Неверный формат токена</b>\n\n"
            "Токен должен выглядеть так:\n"
            "<code>123456789:AAF_xxxxxxxxxxxxxxxxxxxxxxxxxxx</code>\n\n"
            "Получить токен можно у @BotFather → /newbot или /token.",
            parse_mode="HTML",
            reply_markup=_fmt_kb.as_markup(),
        )
        await state.clear()
        return

    info_msg = await message.answer("⏳ Проверяю токен...")

    bot_info = await bot_api.get_me(http, token)
    if not bot_info:
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        _retry_kb = InlineKeyboardBuilder()
        _retry_kb.button(text="❌ Отмена", callback_data=BotCb(action="list", page=0))
        await info_msg.edit_text(
            "❌ Неверный токен или бот недоступен. Попробуйте ещё раз:",
            reply_markup=_retry_kb.as_markup(),
        )
        await state.clear()
        return

    added = await db.add_bot(
        pool,
        token=token,
        bot_id=bot_info["id"],
        username=bot_info.get("username", ""),
        first_name=bot_info.get("first_name", ""),
        added_by=message.from_user.id,
        bot=message.bot,
    )

    await state.clear()

    if added != True:
        safe_uname = (
            (bot_info.get("username") or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        from bot.utils.subscription import is_platform_admin

        if added == "taken":
            err_text = (
                f"🔒 Бот @{safe_uname} уже управляется другим аккаунтом BotMother.\n\n"
                "Каждый Telegram-бот может быть подключён только к одному аккаунту. "
                "Если вы хотите перенести бота — сначала удалите его из другого аккаунта."
            )
        else:
            err_text = f"⚠️ Бот @{safe_uname} уже добавлен в ваш аккаунт."
        await info_msg.edit_text(
            err_text,
            parse_mode="HTML",
            reply_markup=main_menu(is_admin=is_platform_admin(message.from_user.id)),
        )
        return

    raw_label = bot_info.get("username") or bot_info.get("first_name", "")
    safe_label = (
        raw_label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    prefix = "@" if bot_info.get("username") else ""
    await info_msg.edit_text(
        f"✅ Бот <b>{prefix}{safe_label}</b> добавлен!",
        parse_mode="HTML",
        reply_markup=bot_menu(bot_info["id"], username=bot_info.get("username")),
    )


# ── Select bot ────────────────────────────────────────────────────────────


@router.callback_query(BotCb.filter(F.action == "select"))
async def cb_select(
    callback: CallbackQuery, callback_data: BotCb, pool: asyncpg.Pool
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    label = _bot_label(row)
    count = await db.get_audience_count(pool, row["bot_id"])
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if row.get("note"):
        safe_note = (
            row["note"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        note_text = f"\n\n📝 <i>{safe_note}</i>"
    else:
        note_text = ""
    await callback.message.edit_text(
        f"🤖 <b>{safe_label}</b>\n"
        f"ID: <code>{row['bot_id']}</code>\n"
        f"Аудитория: <b>{count}</b> чел."
        f"{note_text}",
        parse_mode="HTML",
        reply_markup=bot_menu(row["bot_id"], username=row.get("username")),
    )


# ── Delete — confirm ──────────────────────────────────────────────────────


@router.callback_query(BotCb.filter(F.action == "delete"))
async def cb_delete(
    callback: CallbackQuery, callback_data: BotCb, pool: asyncpg.Pool
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    safe_label = (
        _bot_label(row).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    await callback.message.edit_text(
        f"🗑 Удалить бота <b>{safe_label}</b>?\n"
        "Аудитория и история рассылок тоже удалятся.",
        parse_mode="HTML",
        reply_markup=confirm_delete(row["bot_id"]),
    )


@router.callback_query(BotCb.filter(F.action == "confirm_delete"))
async def cb_confirm_delete(
    callback: CallbackQuery, callback_data: BotCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    deleted = await db.delete_bot(pool, callback_data.bot_id, callback.from_user.id)
    if deleted:
        await callback.message.edit_text(
            "✅ <b>Бот удалён.</b>", parse_mode="HTML", reply_markup=main_menu()
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Не удалось удалить бота.</b>\n\nВозможно, бот уже был удалён.",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )


# ── Bot Preset Templates ───────────────────────────────────────────────────


@router.callback_query(BotCb.filter(F.action == "pset_list"))
async def cb_pset_list(
    callback: CallbackQuery, callback_data: BotCb, pool: asyncpg.Pool
) -> None:
    from services.preset_templates import get_presets

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    presets = get_presets("bot")
    bot_label = html.escape(f"@{row['username']}" if row.get("username") else row.get("first_name", "бот"))
    kb = InlineKeyboardBuilder()
    for idx, p in enumerate(presets):
        kb.button(
            text=p["name"],
            callback_data=BotCb(action="pset_view", bot_id=callback_data.bot_id, page=idx),
        )
    kb.button(text="◀️ Назад", callback_data=BotCb(action="select", bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📦 <b>Шаблоны для {bot_label}</b>\n\n"
        "Выберите готовый тип бота — будут автоматически настроены авто-ответы, команды и приветственная воронка.\n\n"
        "⚠️ Существующие авто-ответы <b>не удаляются</b>, новые добавляются сверху.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BotCb.filter(F.action == "pset_view"))
async def cb_pset_view(
    callback: CallbackQuery, callback_data: BotCb, pool: asyncpg.Pool
) -> None:
    from services.preset_templates import get_presets

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    presets = get_presets("bot")
    idx = callback_data.page
    if idx < 0 or idx >= len(presets):
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await callback.answer()
    preset = presets[idx]
    tpl = preset["template"]
    cmds = tpl.get("commands", [])
    replies = tpl.get("auto_replies", [])
    steps = tpl.get("funnel_steps", [])
    cmd_list = "\n".join(f"  • /{c['command']} — {c['description']}" for c in cmds[:5]) or "нет"
    reply_list = "\n".join(f"  • {r['keyword']}" for r in replies[:5]) or "нет"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Применить",
        callback_data=BotCb(action="pset_apply", bot_id=callback_data.bot_id, page=idx),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=BotCb(action="pset_list", bot_id=callback_data.bot_id),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        f"📦 <b>{html.escape(preset['name'])}</b>\n"
        f"<i>{html.escape(preset.get('description', ''))}</i>\n\n"
        f"<b>Команды ({len(cmds)}):</b>\n{cmd_list}\n\n"
        f"<b>Авто-ответы ({len(replies)}):</b>\n{reply_list}\n\n"
        f"<b>Приветственная воронка:</b> {len(steps)} шаг(а/ов)\n\n"
        f"Нажмите <b>Применить</b> — всё будет настроено автоматически.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BotCb.filter(F.action == "pset_apply"))
async def cb_pset_apply(
    callback: CallbackQuery,
    callback_data: BotCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    from services.preset_templates import get_presets

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    presets = get_presets("bot")
    idx = callback_data.page
    if idx < 0 or idx >= len(presets):
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await callback.answer("⏳ Применяю шаблон...")
    preset = presets[idx]
    tpl = preset["template"]
    token = row["token"]
    bot_id = callback_data.bot_id

    applied = []
    errors = []

    # 1. Commands
    cmds = tpl.get("commands", [])
    if cmds:
        ok = await bot_api.set_my_commands(http, token, cmds)
        if ok:
            applied.append(f"✅ Команды ({len(cmds)} шт.)")
        else:
            errors.append("❌ Команды — ошибка Telegram API")

    # 2. Welcome message as start auto-reply
    welcome = tpl.get("welcome_message", "")
    if welcome:
        try:
            await db.add_auto_reply(pool, bot_id, "start", None, welcome)
            applied.append("✅ Приветственное сообщение")
        except Exception as e:
            errors.append(f"❌ Приветствие: {e}")

    # 3. Auto-replies
    replies = tpl.get("auto_replies", [])
    for r in replies:
        kw = r.get("keyword") or r.get("trigger", "")
        resp = r.get("response") or r.get("response_text", "")
        if kw and resp:
            try:
                await db.add_auto_reply(pool, bot_id, "keyword", kw, resp)
            except Exception:
                pass
    if replies:
        applied.append(f"✅ Авто-ответы ({len(replies)} шт.)")

    # 4. Funnel
    steps = tpl.get("funnel_steps", [])
    if steps:
        try:
            funnel_row = await db.create_funnel(
                pool, bot_id, f"{preset['name']} (авто)", "start"
            )
            funnel_id = funnel_row["id"]
            for i, step in enumerate(steps):
                msg = step.get("message", "")
                delay_h = step.get("delay_hours", 0)
                if msg:
                    await db.add_funnel_step(
                        pool, funnel_id, i, msg, int(delay_h * 60)
                    )
            applied.append(f"✅ Воронка ({len(steps)} шаг(а))")
        except Exception as e:
            errors.append(f"❌ Воронка: {e}")

    bot_label = html.escape(f"@{row['username']}" if row.get("username") else row.get("first_name", "бот"))
    result_lines = "\n".join(applied + errors)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ К боту", callback_data=BotCb(action="select", bot_id=bot_id)
    )
    kb.adjust(1)
    await callback.message.edit_text(
        f"📦 <b>Шаблон «{html.escape(preset['name'])}» применён к {bot_label}</b>\n\n"
        f"{result_lines}\n\n"
        "<i>Авто-ответы и воронка активны. Команды видны пользователям.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

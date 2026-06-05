"""Multigeo (per-language) bot profile editing."""

import asyncio
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg
from bot.callbacks import MultigeoCb, BotCb
from bot.keyboards import (
    multigeo_menu,
    multigeo_field,
    LANGUAGES,
    subscription_locked_markup,
)
from bot.utils.subscription import require_plan, locked_text
from bot.states import MultigeoEdit
from database import db
from services import bot_api

router = Router()


async def _get_token(pool: asyncpg.Pool, bot_id: int, user_id: int) -> str | None:
    row = await db.get_bot(pool, bot_id, user_id)
    return row["token"] if row else None


def _after_save_markup(bot_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🌍 К мультигео", callback_data=MultigeoCb(action="menu", bot_id=bot_id)
    )
    kb.button(text="◀️ К боту", callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


# ── Menu ──────────────────────────────────────────────────────────────────


@router.callback_query(MultigeoCb.filter(F.action == "menu"))
async def cb_multigeo_menu(
    callback: CallbackQuery, callback_data: MultigeoCb, pool: asyncpg.Pool
) -> None:

    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Мультигео (редактирование по языкам)", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "pro", back_callback=BotCb(action="select", bot_id=callback_data.bot_id)
            ),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"🌍 <b>Мультигео — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Telegram показывает разным людям разные версии бота в зависимости от языка их устройства. Мультигео позволяет задать имя, описание и краткое описание отдельно для русских, англоязычных, испанских и других пользователей.\n\n"
        "💡 <b>Зачем нужно?</b>\n"
        "Если ваш бот работает для нескольких стран — сделайте описание на их языке. Это улучшает позиции в поиске Telegram в этих странах.\n\n"
        "Выберите поле для редактирования:",
        parse_mode="HTML",
        reply_markup=multigeo_menu(callback_data.bot_id),
    )


# ── Names list ────────────────────────────────────────────────────────────


@router.callback_query(MultigeoCb.filter(F.action == "names"))
async def cb_multigeo_names(
    callback: CallbackQuery,
    callback_data: MultigeoCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Загружаю текущие значения…")
    values = await asyncio.gather(
        *(bot_api.get_my_name(http, row["token"], code) for code, _, _ in LANGUAGES),
        return_exceptions=True,
    )
    lang_vals = {
        code: (v if not isinstance(v, Exception) else "")
        for (code, _, _), v in zip(LANGUAGES, values)
    }
    await callback.message.edit_text(
        "🌍 <b>Имена по языкам</b>\n\nВыберите язык для редактирования:",
        parse_mode="HTML",
        reply_markup=multigeo_field(callback_data.bot_id, "name", lang_vals),
    )


# ── Short descriptions list ───────────────────────────────────────────────


@router.callback_query(MultigeoCb.filter(F.action == "short"))
async def cb_multigeo_short(
    callback: CallbackQuery,
    callback_data: MultigeoCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Загружаю текущие значения…")
    values = await asyncio.gather(
        *(
            bot_api.get_my_short_description(http, row["token"], code)
            for code, _, _ in LANGUAGES
        ),
        return_exceptions=True,
    )
    lang_vals = {
        code: (v if not isinstance(v, Exception) else "")
        for (code, _, _), v in zip(LANGUAGES, values)
    }
    await callback.message.edit_text(
        "📋 <b>Краткие описания (about) по языкам</b>\n\nВыберите язык для редактирования:",
        parse_mode="HTML",
        reply_markup=multigeo_field(callback_data.bot_id, "short", lang_vals),
    )


# ── Descriptions list ─────────────────────────────────────────────────────


@router.callback_query(MultigeoCb.filter(F.action == "desc"))
async def cb_multigeo_desc(
    callback: CallbackQuery,
    callback_data: MultigeoCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("⏳ Загружаю текущие значения…")
    values = await asyncio.gather(
        *(
            bot_api.get_my_description(http, row["token"], code)
            for code, _, _ in LANGUAGES
        ),
        return_exceptions=True,
    )
    lang_vals = {
        code: (v if not isinstance(v, Exception) else "")
        for (code, _, _), v in zip(LANGUAGES, values)
    }
    await callback.message.edit_text(
        "📄 <b>Описания по языкам</b>\n\nВыберите язык для редактирования:",
        parse_mode="HTML",
        reply_markup=multigeo_field(callback_data.bot_id, "desc", lang_vals),
    )


# ── Per-language edit: name ───────────────────────────────────────────────


@router.callback_query(MultigeoCb.filter(F.action == "cancel_fsm"))
async def cb_multigeo_cancel_fsm(
    callback: CallbackQuery, callback_data: MultigeoCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(
        "❌ Отменено.",
        parse_mode="HTML",
        reply_markup=_after_save_markup(callback_data.bot_id),
    )


@router.callback_query(MultigeoCb.filter(F.action == "lang_name"))
async def cb_lang_name(
    callback: CallbackQuery,
    callback_data: MultigeoCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Мультигео", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    await callback.answer()
    await state.set_state(MultigeoEdit.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id, lang=callback_data.lang or "")
    lang_label = (callback_data.lang or "").upper()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Отмена",
        callback_data=MultigeoCb(action="cancel_fsm", bot_id=callback_data.bot_id),
    )
    await callback.message.edit_text(
        f"📝 Введите новое имя для языка <code>{lang_label}</code>.\n\n"
        "Отправьте <code>-</code> чтобы сбросить.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MultigeoEdit.waiting_name, F.text)
async def msg_multigeo_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    if not await require_plan(pool, message.from_user.id, "pro"):
        await state.clear()
        await message.answer(
            locked_text("Мультигео", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    data = await state.get_data()
    bot_id = data["bot_id"]
    lang = data["lang"]
    await state.clear()

    token = await _get_token(pool, bot_id, message.from_user.id)
    if not token:
        await message.answer("Бот не найден.")
        return

    value = "" if message.text.strip() == "-" else message.text.strip()
    ok = await bot_api.set_name(http, token, value, language_code=lang)
    await message.answer(
        "✅ Обновлено." if ok else "❌ Не удалось обновить.",
        reply_markup=_after_save_markup(bot_id),
    )


# ── Per-language edit: short description ─────────────────────────────────


@router.callback_query(MultigeoCb.filter(F.action == "lang_short"))
async def cb_lang_short(
    callback: CallbackQuery,
    callback_data: MultigeoCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Мультигео", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    await callback.answer()
    await state.set_state(MultigeoEdit.waiting_short)
    await state.update_data(bot_id=callback_data.bot_id, lang=callback_data.lang or "")
    lang_label = (callback_data.lang or "").upper()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Отмена",
        callback_data=MultigeoCb(action="cancel_fsm", bot_id=callback_data.bot_id),
    )
    await callback.message.edit_text(
        f"📋 Введите краткое описание для языка <code>{lang_label}</code>.\n\n"
        "Отправьте <code>-</code> чтобы сбросить.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MultigeoEdit.waiting_short, F.text)
async def msg_multigeo_short(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    if not await require_plan(pool, message.from_user.id, "pro"):
        await state.clear()
        await message.answer(
            locked_text("Мультигео", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    data = await state.get_data()
    bot_id = data["bot_id"]
    lang = data["lang"]
    await state.clear()

    token = await _get_token(pool, bot_id, message.from_user.id)
    if not token:
        await message.answer("Бот не найден.")
        return

    value = "" if message.text.strip() == "-" else message.text.strip()
    ok = await bot_api.set_short_description(http, token, value, language_code=lang)
    await message.answer(
        "✅ Обновлено." if ok else "❌ Не удалось обновить.",
        reply_markup=_after_save_markup(bot_id),
    )


# ── Per-language edit: description ───────────────────────────────────────


@router.callback_query(MultigeoCb.filter(F.action == "lang_desc"))
async def cb_lang_desc(
    callback: CallbackQuery,
    callback_data: MultigeoCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Мультигео", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    await callback.answer()
    await state.set_state(MultigeoEdit.waiting_desc)
    await state.update_data(bot_id=callback_data.bot_id, lang=callback_data.lang or "")
    lang_label = (callback_data.lang or "").upper()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Отмена",
        callback_data=MultigeoCb(action="cancel_fsm", bot_id=callback_data.bot_id),
    )
    await callback.message.edit_text(
        f"📄 Введите описание для языка <code>{lang_label}</code>.\n\n"
        "Отправьте <code>-</code> чтобы сбросить.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(MultigeoEdit.waiting_desc, F.text)
async def msg_multigeo_desc(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    if not await require_plan(pool, message.from_user.id, "pro"):
        await state.clear()
        await message.answer(
            locked_text("Мультигео", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    data = await state.get_data()
    bot_id = data["bot_id"]
    lang = data["lang"]
    await state.clear()

    token = await _get_token(pool, bot_id, message.from_user.id)
    if not token:
        await message.answer("Бот не найден.")
        return

    value = "" if message.text.strip() == "-" else message.text.strip()
    ok = await bot_api.set_description(http, token, value, language_code=lang)
    await message.answer(
        "✅ Обновлено." if ok else "❌ Не удалось обновить.",
        reply_markup=_after_save_markup(bot_id),
    )

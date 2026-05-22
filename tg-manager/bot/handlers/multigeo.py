"""Multigeo (per-language) bot profile editing."""
import asyncio
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg
from bot.callbacks import MultigeoCb, BotCb
from bot.keyboards import multigeo_menu, multigeo_field, LANGUAGES
from bot.states import MultigeoEdit
from database import db
from services import bot_api

router = Router()


async def _get_token(pool: asyncpg.Pool, bot_id: int, user_id: int) -> str | None:
    row = await db.get_bot(pool, bot_id, user_id)
    return row["token"] if row else None


def _after_save_markup(bot_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🌍 К мультигео", callback_data=MultigeoCb(action="menu", bot_id=bot_id))
    kb.button(text="◀️ К боту",      callback_data=BotCb(action="select", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


# ── Menu ──────────────────────────────────────────────────────────────────

@router.callback_query(MultigeoCb.filter(F.action == "menu"))
async def cb_multigeo_menu(callback: CallbackQuery, callback_data: MultigeoCb,
                            pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"🌍 <b>Мультигео {label}</b>\n\nВыберите поле для редактирования по языкам:",
        parse_mode="HTML",
        reply_markup=multigeo_menu(callback_data.bot_id),
    )
    await callback.answer()


# ── Names list ────────────────────────────────────────────────────────────

@router.callback_query(MultigeoCb.filter(F.action == "names"))
async def cb_multigeo_names(callback: CallbackQuery, callback_data: MultigeoCb,
                             pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    values = await asyncio.gather(
        *(bot_api.get_my_name(http, row["token"], code) for code, _, _ in LANGUAGES)
    )
    lang_vals = {code: val for (code, _, _), val in zip(LANGUAGES, values)}
    await callback.message.edit_text(
        "🌍 <b>Имена по языкам</b>\n\nВыберите язык для редактирования:",
        parse_mode="HTML",
        reply_markup=multigeo_field(callback_data.bot_id, "name", lang_vals),
    )
    await callback.answer()


# ── Short descriptions list ───────────────────────────────────────────────

@router.callback_query(MultigeoCb.filter(F.action == "short"))
async def cb_multigeo_short(callback: CallbackQuery, callback_data: MultigeoCb,
                             pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    values = await asyncio.gather(
        *(bot_api.get_my_short_description(http, row["token"], code) for code, _, _ in LANGUAGES)
    )
    lang_vals = {code: val for (code, _, _), val in zip(LANGUAGES, values)}
    await callback.message.edit_text(
        "📋 <b>Краткие описания (about) по языкам</b>\n\nВыберите язык для редактирования:",
        parse_mode="HTML",
        reply_markup=multigeo_field(callback_data.bot_id, "short", lang_vals),
    )
    await callback.answer()


# ── Descriptions list ─────────────────────────────────────────────────────

@router.callback_query(MultigeoCb.filter(F.action == "desc"))
async def cb_multigeo_desc(callback: CallbackQuery, callback_data: MultigeoCb,
                            pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    values = await asyncio.gather(
        *(bot_api.get_my_description(http, row["token"], code) for code, _, _ in LANGUAGES)
    )
    lang_vals = {code: val for (code, _, _), val in zip(LANGUAGES, values)}
    await callback.message.edit_text(
        "📄 <b>Описания по языкам</b>\n\nВыберите язык для редактирования:",
        parse_mode="HTML",
        reply_markup=multigeo_field(callback_data.bot_id, "desc", lang_vals),
    )
    await callback.answer()


# ── Per-language edit: name ───────────────────────────────────────────────

@router.callback_query(MultigeoCb.filter(F.action == "lang_name"))
async def cb_lang_name(callback: CallbackQuery, callback_data: MultigeoCb,
                        state: FSMContext) -> None:
    await state.set_state(MultigeoEdit.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id, lang=callback_data.lang)
    lang_label = callback_data.lang.upper()
    await callback.message.edit_text(
        f"📝 Введите новое имя для языка <code>{lang_label}</code>.\n\n"
        "Отправьте <code>-</code> чтобы сбросить.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(MultigeoEdit.waiting_name)
async def msg_multigeo_name(message: Message, state: FSMContext,
                             pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
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
async def cb_lang_short(callback: CallbackQuery, callback_data: MultigeoCb,
                         state: FSMContext) -> None:
    await state.set_state(MultigeoEdit.waiting_short)
    await state.update_data(bot_id=callback_data.bot_id, lang=callback_data.lang)
    lang_label = callback_data.lang.upper()
    await callback.message.edit_text(
        f"📋 Введите краткое описание для языка <code>{lang_label}</code>.\n\n"
        "Отправьте <code>-</code> чтобы сбросить.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(MultigeoEdit.waiting_short)
async def msg_multigeo_short(message: Message, state: FSMContext,
                              pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
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
async def cb_lang_desc(callback: CallbackQuery, callback_data: MultigeoCb,
                        state: FSMContext) -> None:
    await state.set_state(MultigeoEdit.waiting_desc)
    await state.update_data(bot_id=callback_data.bot_id, lang=callback_data.lang)
    lang_label = callback_data.lang.upper()
    await callback.message.edit_text(
        f"📄 Введите описание для языка <code>{lang_label}</code>.\n\n"
        "Отправьте <code>-</code> чтобы сбросить.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(MultigeoEdit.waiting_desc)
async def msg_multigeo_desc(message: Message, state: FSMContext,
                             pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
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

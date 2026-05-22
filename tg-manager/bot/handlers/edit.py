"""Bot profile editing: name, description, short description, photo (incl. per-GEO)."""
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import EditCb, BotCb
from bot.keyboards import edit_menu, back_to_bot
from bot.states import EditProfile
from database import db
from services import bot_api

router = Router()

_LANG_HINT = (
    "Введите код языка (например: <code>ru</code>, <code>en</code>, <code>uk</code>, "
    "<code>de</code>) или <code>-</code> чтобы сбросить до дефолтного."
)


async def _get_token(pool: asyncpg.Pool, bot_id: int, user_id: int) -> str | None:
    row = await db.get_bot(pool, bot_id, user_id)
    return row["token"] if row else None


# ── Edit menu ─────────────────────────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "menu"))
async def cb_edit_menu(callback: CallbackQuery, callback_data: EditCb,
                        pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"✏️ <b>Редактирование {label}</b>\n\nВыберите что изменить:",
        parse_mode="HTML",
        reply_markup=edit_menu(callback_data.bot_id),
    )
    await callback.answer()


# ── Name (default language) ───────────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "name"))
async def cb_name(callback: CallbackQuery, callback_data: EditCb,
                   state: FSMContext) -> None:
    await state.set_state(EditProfile.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text("📝 Введите новое имя бота (до 64 символов):")
    await callback.answer()


@router.message(EditProfile.waiting_name)
async def msg_name(message: Message, state: FSMContext,
                   pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    token = await _get_token(pool, data["bot_id"], message.from_user.id)
    if not token:
        await state.clear()
        return

    ok = await bot_api.set_name(http, token, message.text.strip())
    await state.clear()
    await message.answer(
        "✅ Имя обновлено." if ok else "❌ Не удалось обновить имя.",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Name by GEO (localised) ───────────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "name_lang"))
async def cb_name_lang(callback: CallbackQuery, callback_data: EditCb,
                        state: FSMContext) -> None:
    await state.set_state(EditProfile.waiting_name_lang)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(f"🌍 <b>Имя по языку</b>\n\n{_LANG_HINT}",
                                      parse_mode="HTML")
    await callback.answer()


@router.message(EditProfile.waiting_name_lang)
async def msg_name_lang(message: Message, state: FSMContext) -> None:
    lang = message.text.strip()
    await state.update_data(lang=lang)
    await state.set_state(EditProfile.waiting_localized_name)
    await message.answer(f"📝 Введите имя для языка <code>{lang}</code>:",
                          parse_mode="HTML")


@router.message(EditProfile.waiting_localized_name)
async def msg_localized_name(message: Message, state: FSMContext,
                               pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    token = await _get_token(pool, data["bot_id"], message.from_user.id)
    if not token:
        await state.clear()
        return

    lang = "" if data["lang"] == "-" else data["lang"]
    ok = await bot_api.set_name(http, token, message.text.strip(), language_code=lang)
    await state.clear()
    await message.answer(
        "✅ Локализованное имя обновлено." if ok else "❌ Ошибка при обновлении.",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Description (default) ────────────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "desc"))
async def cb_desc(callback: CallbackQuery, callback_data: EditCb,
                   state: FSMContext) -> None:
    await state.set_state(EditProfile.waiting_desc)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text("📄 Введите новое описание бота (до 512 символов):")
    await callback.answer()


@router.message(EditProfile.waiting_desc)
async def msg_desc(message: Message, state: FSMContext,
                   pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    token = await _get_token(pool, data["bot_id"], message.from_user.id)
    if not token:
        await state.clear()
        return
    ok = await bot_api.set_description(http, token, message.text.strip())
    await state.clear()
    await message.answer(
        "✅ Описание обновлено." if ok else "❌ Не удалось обновить описание.",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Description by GEO ────────────────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "desc_lang"))
async def cb_desc_lang(callback: CallbackQuery, callback_data: EditCb,
                        state: FSMContext) -> None:
    await state.set_state(EditProfile.waiting_desc_lang)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(f"🌍 <b>Описание по языку</b>\n\n{_LANG_HINT}",
                                      parse_mode="HTML")
    await callback.answer()


@router.message(EditProfile.waiting_desc_lang)
async def msg_desc_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(EditProfile.waiting_localized_desc)
    await message.answer("📄 Введите описание:")


@router.message(EditProfile.waiting_localized_desc)
async def msg_localized_desc(message: Message, state: FSMContext,
                               pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    token = await _get_token(pool, data["bot_id"], message.from_user.id)
    if not token:
        await state.clear()
        return
    lang = "" if data["lang"] == "-" else data["lang"]
    ok = await bot_api.set_description(http, token, message.text.strip(), language_code=lang)
    await state.clear()
    await message.answer(
        "✅ Локализованное описание обновлено." if ok else "❌ Ошибка при обновлении.",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Short description (default) ───────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "short"))
async def cb_short(callback: CallbackQuery, callback_data: EditCb,
                    state: FSMContext) -> None:
    await state.set_state(EditProfile.waiting_short)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text("📃 Введите краткое описание (до 120 символов):")
    await callback.answer()


@router.message(EditProfile.waiting_short)
async def msg_short(message: Message, state: FSMContext,
                    pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    token = await _get_token(pool, data["bot_id"], message.from_user.id)
    if not token:
        await state.clear()
        return
    ok = await bot_api.set_short_description(http, token, message.text.strip())
    await state.clear()
    await message.answer(
        "✅ Краткое описание обновлено." if ok else "❌ Ошибка при обновлении.",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Short description by GEO ──────────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "short_lang"))
async def cb_short_lang(callback: CallbackQuery, callback_data: EditCb,
                         state: FSMContext) -> None:
    await state.set_state(EditProfile.waiting_short_lang)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(f"🌍 <b>Краткое описание по языку</b>\n\n{_LANG_HINT}",
                                      parse_mode="HTML")
    await callback.answer()


@router.message(EditProfile.waiting_short_lang)
async def msg_short_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(EditProfile.waiting_localized_short)
    await message.answer("📃 Введите краткое описание:")


@router.message(EditProfile.waiting_localized_short)
async def msg_localized_short(message: Message, state: FSMContext,
                                pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    token = await _get_token(pool, data["bot_id"], message.from_user.id)
    if not token:
        await state.clear()
        return
    lang = "" if data["lang"] == "-" else data["lang"]
    ok = await bot_api.set_short_description(http, token, message.text.strip(), language_code=lang)
    await state.clear()
    await message.answer(
        "✅ Краткое описание обновлено." if ok else "❌ Ошибка при обновлении.",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ── Photo ─────────────────────────────────────────────────────────────────

@router.callback_query(EditCb.filter(F.action == "photo"))
async def cb_photo(callback: CallbackQuery, callback_data: EditCb,
                    state: FSMContext) -> None:
    await state.set_state(EditProfile.waiting_photo)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🖼 Отправьте новое фото для бота.\n\n"
        "Отправьте как фото (не файл), квадратное изображение минимум 160×160."
    )
    await callback.answer()


@router.message(EditProfile.waiting_photo, F.photo)
async def msg_photo(message: Message, state: FSMContext,
                    bot: Bot, pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    token = await _get_token(pool, data["bot_id"], message.from_user.id)
    if not token:
        await state.clear()
        return

    # Download photo from Telegram
    photo = message.photo[-1]  # highest resolution
    file = await bot.get_file(photo.file_id)
    file_data = await bot.download_file(file.file_path)
    photo_bytes = file_data.read()

    ok = await bot_api.set_photo(http, token, photo_bytes)
    await state.clear()
    await message.answer(
        "✅ Фото обновлено." if ok else "❌ Не удалось обновить фото.",
        reply_markup=back_to_bot(data["bot_id"]),
    )


@router.message(EditProfile.waiting_photo)
async def msg_photo_wrong(message: Message) -> None:
    await message.answer("Пожалуйста, отправьте изображение как фото, не как файл.")


@router.callback_query(EditCb.filter(F.action == "del_photo"))
async def cb_del_photo(callback: CallbackQuery, callback_data: EditCb,
                        pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    ok = await bot_api.delete_my_photo(http, row["token"])
    if ok:
        await callback.message.edit_text(
            "✅ Фото удалено.",
            reply_markup=edit_menu(callback_data.bot_id),
        )
        await callback.answer("✅ Фото удалено.")
    else:
        await callback.answer("❌ Не удалось удалить фото.", show_alert=True)

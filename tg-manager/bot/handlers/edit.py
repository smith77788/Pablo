"""Bot profile editing: name, description, short description, photo (default language only)."""

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import EditCb
from bot.keyboards import edit_menu, back_to_bot
from bot.states import EditProfile, UpdateToken
from database import db
from services import bot_api

router = Router()


async def _get_token(pool: asyncpg.Pool, bot_id: int, user_id: int) -> str | None:
    row = await db.get_bot(pool, bot_id, user_id)
    return row["token"] if row else None


# ── Edit menu ─────────────────────────────────────────────────────────────


@router.callback_query(EditCb.filter(F.action == "menu"))
async def cb_edit_menu(
    callback: CallbackQuery, callback_data: EditCb, pool: asyncpg.Pool
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"✏️ <b>Редактирование {label}</b>\n\nВыберите что изменить:",
        parse_mode="HTML",
        reply_markup=edit_menu(callback_data.bot_id),
    )
    await callback.answer()


# ── Name (default language) ───────────────────────────────────────────────


@router.callback_query(EditCb.filter(F.action == "name"))
async def cb_name(
    callback: CallbackQuery, callback_data: EditCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(EditProfile.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text("📝 Введите новое имя бота (до 64 символов):")


@router.message(EditProfile.waiting_name, F.text)
async def msg_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
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


# ── Description (default) ────────────────────────────────────────────────


@router.callback_query(EditCb.filter(F.action == "desc"))
async def cb_desc(
    callback: CallbackQuery, callback_data: EditCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(EditProfile.waiting_desc)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "📄 Введите новое описание бота (до 512 символов):"
    )


@router.message(EditProfile.waiting_desc, F.text)
async def msg_desc(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
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


# ── Short description (default) ───────────────────────────────────────────


@router.callback_query(EditCb.filter(F.action == "short"))
async def cb_short(
    callback: CallbackQuery, callback_data: EditCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(EditProfile.waiting_short)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text("📃 Введите краткое описание (до 120 символов):")


@router.message(EditProfile.waiting_short, F.text)
async def msg_short(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
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


# ── Photo ─────────────────────────────────────────────────────────────────


@router.callback_query(EditCb.filter(F.action == "photo"))
async def cb_photo(
    callback: CallbackQuery, callback_data: EditCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(EditProfile.waiting_photo)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🖼 Отправьте новое фото для бота.\n\n"
        "Отправьте как фото (не файл), квадратное изображение минимум 160×160."
    )


@router.message(EditProfile.waiting_photo, F.photo)
async def msg_photo(
    message: Message,
    state: FSMContext,
    bot: Bot,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
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
async def cb_del_photo(
    callback: CallbackQuery,
    callback_data: EditCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:

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


# ── Update token ──────────────────────────────────────────────────────────


@router.callback_query(EditCb.filter(F.action == "update_token"))
async def cb_update_token(
    callback: CallbackQuery, callback_data: EditCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(UpdateToken.waiting_token)
    await state.update_data(old_bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🔑 <b>Обновление токена</b>\n\n"
        "Отправьте новый токен бота.\n"
        "<i>Токен должен быть от того же или нового бота BotFather.</i>",
        parse_mode="HTML",
    )


@router.message(UpdateToken.waiting_token, F.text)
async def msg_update_token(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    token = message.text.strip() if message.text else ""
    if not token or ":" not in token:
        await message.answer("❌ Неверный формат токена. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    old_bot_id = data["old_bot_id"]

    # Validate new token via API
    info = await bot_api.get_me(http, token)
    if not info:
        await message.answer("❌ Токен недействителен. Проверьте и попробуйте снова:")
        return

    new_bot_id = info["id"]
    username = info.get("username", "")
    first_name = info.get("first_name", "")

    await state.clear()
    await db.update_bot_token(
        pool, old_bot_id, message.from_user.id, token, new_bot_id, username, first_name
    )

    label = f"@{username}" if username else first_name
    await message.answer(
        f"✅ Токен обновлён!\n\nБот: <b>{label}</b>",
        parse_mode="HTML",
        reply_markup=back_to_bot(new_bot_id),
    )


# ── Health check ──────────────────────────────────────────────────────────


@router.callback_query(EditCb.filter(F.action == "health"))
async def cb_health(
    callback: CallbackQuery,
    callback_data: EditCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.answer("⏳ Проверяю…")
    info = await bot_api.get_me(http, row["token"])
    if info:
        label = (
            f"@{info.get('username', '')}"
            if info.get("username")
            else info.get("first_name", str(info["id"]))
        )
        wh = await bot_api.get_webhook_info(http, row["token"])
        webhook_url = wh.get("url") if wh else ""
        wh_text = (
            f"Вебхук: <code>{webhook_url}</code>"
            if webhook_url
            else "Вебхук: не установлен (polling)"
        )
        await callback.message.edit_text(
            f"✅ <b>Бот работает нормально</b>\n\n"
            f"Имя: <b>{info.get('first_name', '')}</b>\n"
            f"Username: {label}\n"
            f"ID: <code>{info['id']}</code>\n"
            f"Принимает сообщения: {'✅' if not info.get('has_private_forwards') else '⚠️'}\n"
            f"{wh_text}",
            parse_mode="HTML",
            reply_markup=edit_menu(callback_data.bot_id),
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Бот недоступен!</b>\n\n"
            "Токен недействителен или бот заблокирован BotFather.\n"
            "Обновите токен или проверьте в BotFather.",
            parse_mode="HTML",
            reply_markup=edit_menu(callback_data.bot_id),
        )

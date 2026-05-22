"""Webhook management for managed bots."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import WebhookCb, BotCb
from bot.keyboards import webhook_menu, back_to_bot
from bot.states import SetWebhook
from database import db
from services import bot_api

router = Router()


@router.callback_query(WebhookCb.filter(F.action == "menu"))
async def cb_webhook_menu(callback: CallbackQuery, callback_data: WebhookCb,
                           pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    info = await bot_api.get_webhook_info(http, row["token"])
    url = info.get("url", "") or "не установлен"
    pending = info.get("pending_update_count", 0)
    last_err = info.get("last_error_message", "")
    label = f"@{row['username']}" if row["username"] else row["first_name"]

    text = (
        f"🔗 <b>Вебхук {label}</b>\n\n"
        f"URL: <code>{url}</code>\n"
        f"Ожидающих: {pending}"
    )
    if last_err:
        text += f"\n⚠️ Последняя ошибка: {last_err}"

    await callback.message.edit_text(text, parse_mode="HTML",
                                      reply_markup=webhook_menu(callback_data.bot_id))
    await callback.answer()


@router.callback_query(WebhookCb.filter(F.action == "set"))
async def cb_webhook_set(callback: CallbackQuery, callback_data: WebhookCb,
                          state: FSMContext) -> None:
    await state.set_state(SetWebhook.waiting_url)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🔗 Введите URL вебхука:\n\n"
        "<code>https://yourdomain.com/webhook/bot123</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SetWebhook.waiting_url)
async def msg_webhook_url(message: Message, state: FSMContext,
                           pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    row = await db.get_bot(pool, data["bot_id"], message.from_user.id)
    if not row:
        await state.clear()
        return

    url = message.text.strip()
    if not url.startswith("https://"):
        await message.answer("⚠️ URL должен начинаться с https://")
        return

    result = await bot_api.set_webhook(http, row["token"], url)
    await state.clear()

    if result.get("ok"):
        await message.answer(
            f"✅ Вебхук установлен:\n<code>{url}</code>",
            parse_mode="HTML",
            reply_markup=back_to_bot(data["bot_id"]),
        )
    else:
        desc = result.get("description", "неизвестная ошибка")
        await message.answer(
            f"❌ Ошибка: {desc}",
            reply_markup=back_to_bot(data["bot_id"]),
        )


@router.callback_query(WebhookCb.filter(F.action == "delete"))
async def cb_webhook_delete(callback: CallbackQuery, callback_data: WebhookCb,
                              pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    result = await bot_api.delete_webhook(http, row["token"])
    if result.get("ok"):
        await callback.message.edit_text(
            "✅ Вебхук удалён.",
            reply_markup=back_to_bot(callback_data.bot_id),
        )
    else:
        await callback.answer("❌ Не удалось удалить вебхук.", show_alert=True)
    await callback.answer()

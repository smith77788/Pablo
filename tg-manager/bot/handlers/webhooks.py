"""Webhook management for managed bots."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
import aiohttp
import asyncpg
from bot.callbacks import WebhookCb
from bot.keyboards import webhook_menu, back_to_bot
from database import db
from services import bot_api

router = Router()


@router.callback_query(WebhookCb.filter(F.action == "menu"))
async def cb_webhook_menu(callback: CallbackQuery, callback_data: WebhookCb,
                           pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()

    label = f"@{row['username']}" if row["username"] else row["first_name"]
    text = (
        f"🔗 <b>Webhook — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Webhook — адрес вашего сервера, куда Telegram отправляет все сообщения боту в реальном времени.\n\n"
        "💡 <b>Как использовать:</b>\n"
        "• Проверьте текущий URL и статус очереди\n"
        "• Если бот «завис» — удалите вебхук, это сбросит очередь\n"
        "• Не нужен для большинства функций TG Manager (используется polling)"
    )
    await callback.message.edit_text(text, parse_mode="HTML",
                                      reply_markup=webhook_menu(callback_data.bot_id))


@router.callback_query(WebhookCb.filter(F.action == "info"))
async def cb_webhook_info(callback: CallbackQuery, callback_data: WebhookCb,
                           pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()

    info = await bot_api.get_webhook_info(http, row["token"])
    url = info.get("url", "") or "не установлен"
    pending = info.get("pending_update_count", 0)
    last_err = info.get("last_error_message", "")
    max_conn = info.get("max_connections", "—")
    allowed = ", ".join(info.get("allowed_updates", [])) or "все"

    label = f"@{row['username']}" if row["username"] else row["first_name"]
    text = (
        f"🔗 <b>Вебхук {label}</b>\n\n"
        f"URL: <code>{url}</code>\n"
        f"Ожидающих: {pending}\n"
        f"Max connections: {max_conn}\n"
        f"Allowed updates: {allowed}"
    )
    if last_err:
        text += f"\n⚠️ Последняя ошибка: {last_err}"

    await callback.message.edit_text(text, parse_mode="HTML",
                                      reply_markup=webhook_menu(callback_data.bot_id))


@router.callback_query(WebhookCb.filter(F.action == "disable"))
async def cb_webhook_disable(callback: CallbackQuery, callback_data: WebhookCb,
                              pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()

    result = await bot_api.delete_webhook(http, row["token"])
    if result.get("ok"):
        await callback.message.edit_text(
            "✅ Вебхук отключён.",
            reply_markup=back_to_bot(callback_data.bot_id),
        )
        await callback.answer("✅ Готово.")
    else:
        await callback.answer("❌ Не удалось отключить вебхук.", show_alert=True)

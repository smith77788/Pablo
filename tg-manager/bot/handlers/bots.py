"""Add, list, select, delete managed bots."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
import aiohttp
import asyncpg
from bot.callbacks import BotCb, SubCb
from bot.keyboards import bots_list, bot_menu, confirm_delete, main_menu, subscription_locked_markup
from bot.states import AddBot
from bot.utils.subscription import get_bot_limit
from database import db
from services import bot_api
from config import ADMIN_IDS

router = Router()


def _is_admin(uid: int) -> bool:
    return not ADMIN_IDS or uid in ADMIN_IDS


def _bot_label(row: asyncpg.Record) -> str:
    return f"@{row['username']}" if row["username"] else row["first_name"]


# ── List ──────────────────────────────────────────────────────────────────

@router.callback_query(BotCb.filter(F.action == "list"))
async def cb_list(callback: CallbackQuery, callback_data: BotCb, pool: asyncpg.Pool) -> None:

    await callback.answer()
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    bots = await db.get_bots(pool, callback.from_user.id)
    if not bots:
        await callback.message.edit_text(
            "У вас пока нет добавленных ботов.\nНажмите «➕ Добавить бота».",
            reply_markup=main_menu(),
        )
    else:
        await callback.message.edit_text(
            f"🤖 <b>Ваши боты</b> — {len(bots)} шт.",
            parse_mode="HTML",
            reply_markup=bots_list(bots, callback_data.page),
        )
    await callback.answer()


# ── Add — step 1: ask token ───────────────────────────────────────────────

@router.callback_query(BotCb.filter(F.action == "add"))
async def cb_add(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔️ Доступ запрещён.", show_alert=True)
        return
    limit = await get_bot_limit(pool, callback.from_user.id)
    current_bots = await db.get_bots(pool, callback.from_user.id)
    if len(current_bots) >= limit:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        kb = InlineKeyboardBuilder()
        kb.button(text="⭐ Улучшить подписку", callback_data=SubCb(action="menu"))
        await callback.message.edit_text(
            f"⛔️ <b>Достигнут лимит ботов</b>\n\n"
            f"На вашем тарифе можно добавить максимум <b>{limit}</b> бот(ов).\n"
            f"У вас уже добавлено: <b>{len(current_bots)}</b>\n\n"
            "Улучшите подписку, чтобы добавить больше ботов:\n"
            "⭐ Starter — до 10 ботов\n"
            "🚀 Pro — до 30 ботов\n"
            "👑 Enterprise — без ограничений",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    await state.set_state(AddBot.waiting_token)
    await callback.message.edit_text(
        "🔑 <b>Добавление бота</b>\n\n"
        "Отправьте токен бота (получить у @BotFather):\n\n"
        "<code>123456789:AAF...</code>\n\n"
        f"<i>Добавлено {len(current_bots)} из {limit} доступных ботов</i>",
        parse_mode="HTML",
    )


# ── Add — step 2: receive token ───────────────────────────────────────────

@router.message(AddBot.waiting_token, F.text)
async def msg_token(message: Message, state: FSMContext,
                    pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    token = message.text.strip()
    info_msg = await message.answer("⏳ Проверяю токен...")

    bot_info = await bot_api.get_me(http, token)
    if not bot_info:
        await info_msg.edit_text(
            "❌ Неверный токен или бот недоступен. Попробуйте ещё раз:"
        )
        return

    added = await db.add_bot(
        pool,
        token=token,
        bot_id=bot_info["id"],
        username=bot_info.get("username", ""),
        first_name=bot_info.get("first_name", ""),
        added_by=message.from_user.id,
    )

    await state.clear()

    if not added:
        await info_msg.edit_text(
            f"⚠️ Бот @{bot_info.get('username')} уже добавлен.",
            reply_markup=main_menu(),
        )
        return

    await info_msg.edit_text(
        f"✅ Бот <b>@{bot_info.get('username', bot_info['first_name'])}</b> добавлен!",
        parse_mode="HTML",
        reply_markup=bot_menu(bot_info["id"], username=bot_info.get("username")),
    )


# ── Select bot ────────────────────────────────────────────────────────────

@router.callback_query(BotCb.filter(F.action == "select"))
async def cb_select(callback: CallbackQuery, callback_data: BotCb,
                    pool: asyncpg.Pool) -> None:

    await callback.answer()
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    label = _bot_label(row)
    count = await db.get_audience_count(pool, row["bot_id"])
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if row.get("note"):
        safe_note = row["note"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
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
async def cb_delete(callback: CallbackQuery, callback_data: BotCb,
                    pool: asyncpg.Pool) -> None:

    await callback.answer()
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.message.edit_text(
        f"🗑 Удалить бота <b>{_bot_label(row)}</b>?\n"
        "Аудитория и история рассылок тоже удалятся.",
        parse_mode="HTML",
        reply_markup=confirm_delete(row["bot_id"]),
    )
    await callback.answer()


@router.callback_query(BotCb.filter(F.action == "confirm_delete"))
async def cb_confirm_delete(callback: CallbackQuery, callback_data: BotCb,
                             pool: asyncpg.Pool) -> None:

    deleted = await db.delete_bot(pool, callback_data.bot_id, callback.from_user.id)
    if deleted:
        await callback.answer("✅ Бот удалён.")
        await callback.message.edit_text("✅ Бот удалён.", reply_markup=main_menu())
    else:
        await callback.answer("Не удалось удалить.", show_alert=True)

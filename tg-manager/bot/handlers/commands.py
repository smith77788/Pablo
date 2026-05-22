"""Manage bot commands (set/view/delete, with language support)."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
import aiohttp
import asyncpg
from bot.callbacks import CommandsCb, BotCb
from bot.keyboards import commands_menu, back_to_bot
from bot.states import SetCommands
from database import db
from services import bot_api

router = Router()


def _parse_commands(text: str) -> list[dict] | None:
    commands = []
    for line in text.strip().splitlines():
        line = line.strip().lstrip("/")
        if " - " in line:
            cmd, _, desc = line.partition(" - ")
        elif " — " in line:
            cmd, _, desc = line.partition(" — ")
        else:
            return None
        cmd = cmd.strip().lower()
        desc = desc.strip()
        if not cmd or not desc or len(cmd) > 32 or len(desc) > 256:
            return None
        commands.append({"command": cmd, "description": desc})
    return commands or None


@router.callback_query(CommandsCb.filter(F.action == "menu"))
async def cb_commands_menu(callback: CallbackQuery, callback_data: CommandsCb,
                            pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    cmds = await bot_api.get_my_commands(http, row["token"])
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    if cmds:
        lines = "\n".join(f"/{c['command']} — {c['description']}" for c in cmds)
        text = f"🤖 <b>Команды {label}</b>\n\n{lines}"
    else:
        text = f"🤖 <b>Команды {label}</b>\n\nКоманды не заданы."
    await callback.message.edit_text(text, parse_mode="HTML",
                                      reply_markup=commands_menu(callback_data.bot_id))
    await callback.answer()


@router.callback_query(CommandsCb.filter(F.action == "set"))
async def cb_commands_set(callback: CallbackQuery, callback_data: CommandsCb,
                           state: FSMContext) -> None:
    await state.set_state(SetCommands.waiting_commands)
    await state.update_data(bot_id=callback_data.bot_id, lang="")
    await callback.message.edit_text(
        "🤖 <b>Установка команд (по умолчанию)</b>\n\n"
        "Отправьте список команд, каждая с новой строки:\n\n"
        "<code>start - Главное меню\n"
        "help - Помощь\n"
        "about - О боте</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(CommandsCb.filter(F.action == "set_lang"))
async def cb_commands_set_lang(callback: CallbackQuery, callback_data: CommandsCb,
                                state: FSMContext) -> None:
    await state.set_state(SetCommands.waiting_lang)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🌍 <b>Команды по языку</b>\n\n"
        "Введите код языка (<code>ru</code>, <code>en</code>, <code>uk</code>, <code>de</code>…):",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SetCommands.waiting_lang)
async def msg_commands_lang(message: Message, state: FSMContext) -> None:
    lang = message.text.strip()
    await state.update_data(lang=lang)
    await state.set_state(SetCommands.waiting_commands)
    await message.answer(
        f"🤖 Команды для языка <code>{lang}</code>:\n\n"
        "<code>start - Главное меню\n"
        "help - Помощь</code>",
        parse_mode="HTML",
    )


@router.message(SetCommands.waiting_commands)
async def msg_commands_text(message: Message, state: FSMContext,
                             pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    bot_id = data["bot_id"]
    lang = data.get("lang", "")
    await state.clear()

    commands = _parse_commands(message.text or "")
    if not commands:
        await message.answer(
            "❌ Неверный формат. Каждая строка должна быть:\n"
            "<code>/команда - Описание</code>",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id),
        )
        return

    row = await db.get_bot(pool, bot_id, message.from_user.id)
    if not row:
        await message.answer("Бот не найден.")
        return

    ok = await bot_api.set_my_commands(http, row["token"], commands, lang)
    if ok:
        lines = "\n".join(f"/{c['command']} — {c['description']}" for c in commands)
        label_lang = f" [{lang}]" if lang else " [default]"
        await message.answer(
            f"✅ Команды{label_lang} установлены:\n\n{lines}",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id),
        )
    else:
        await message.answer(
            "❌ Не удалось установить команды.",
            reply_markup=back_to_bot(bot_id),
        )


@router.callback_query(CommandsCb.filter(F.action == "delete"))
async def cb_commands_delete(callback: CallbackQuery, callback_data: CommandsCb,
                              pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    ok = await bot_api.delete_my_commands(http, row["token"])
    if ok:
        await callback.message.edit_text(
            "🤖 Команды удалены.",
            reply_markup=commands_menu(callback_data.bot_id),
        )
        await callback.answer("✅ Команды удалены.")
    else:
        await callback.answer("❌ Не удалось удалить команды.", show_alert=True)

"""Manage bot commands (set/view/delete)."""
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

    await callback.answer()
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


@router.callback_query(CommandsCb.filter(F.action == "add"))
async def cb_commands_add(callback: CallbackQuery, callback_data: CommandsCb,
                           state: FSMContext) -> None:
    await state.set_state(SetCommands.waiting_add)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "➕ <b>Добавить команду</b>\n\n"
        "Отправьте одну строку в формате:\n\n"
        "<code>команда - Описание команды</code>\n\n"
        "Например: <code>help - Помощь</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SetCommands.waiting_add, F.text)
async def msg_commands_add(message: Message, state: FSMContext,
                            pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    bot_id = data["bot_id"]
    await state.clear()

    new_cmds = _parse_commands(message.text or "")
    if not new_cmds or len(new_cmds) != 1:
        await message.answer(
            "❌ Неверный формат. Введите одну строку:\n"
            "<code>/команда - Описание</code>",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id),
        )
        return

    row = await db.get_bot(pool, bot_id, message.from_user.id)
    if not row:
        await message.answer("Бот не найден.")
        return

    # Load existing commands and merge
    existing = await bot_api.get_my_commands(http, row["token"])
    new_cmd_name = new_cmds[0]["command"]
    merged = [c for c in existing if c["command"] != new_cmd_name]
    merged.append(new_cmds[0])

    ok = await bot_api.set_my_commands(http, row["token"], merged)
    if ok:
        lines = "\n".join(f"/{c['command']} — {c['description']}" for c in merged)
        await message.answer(
            f"✅ Команда добавлена. Текущий список:\n\n{lines}",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id),
        )
    else:
        await message.answer(
            "❌ Не удалось обновить команды.",
            reply_markup=back_to_bot(bot_id),
        )


@router.callback_query(CommandsCb.filter(F.action == "set_all"))
async def cb_commands_set_all(callback: CallbackQuery, callback_data: CommandsCb,
                               state: FSMContext) -> None:
    await state.set_state(SetCommands.waiting_commands)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "📋 <b>Задать весь список команд</b>\n\n"
        "Отправьте список команд, каждая с новой строки:\n\n"
        "<code>start - Главное меню\n"
        "help - Помощь\n"
        "about - О боте</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(SetCommands.waiting_commands, F.text)
async def msg_commands_set_all(message: Message, state: FSMContext,
                                pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    bot_id = data["bot_id"]
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

    ok = await bot_api.set_my_commands(http, row["token"], commands)
    if ok:
        lines = "\n".join(f"/{c['command']} — {c['description']}" for c in commands)
        await message.answer(
            f"✅ Команды установлены:\n\n{lines}",
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

    await callback.answer()
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

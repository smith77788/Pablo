"""Мои данные: выгрузка (/my_data) и удаление (/delete_my_data) по запросу.

Требование 152-ФЗ и GDPR: человек может получить свои данные и потребовать их
удаления. Логика — services/owner_data.py; здесь только вход и подтверждение.

Удаление необратимо, поэтому в два шага и без состояния в памяти процесса:
первый вызов объясняет последствия, второй — `/delete_my_data ПОДТВЕРЖДАЮ`.
"""
from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from bot.utils.op_helpers import terminal_kb
from services import owner_data

log = logging.getLogger(__name__)
router = Router()

CONFIRM_WORD = "ПОДТВЕРЖДАЮ"


@router.message(Command("my_data"), F.chat.type == "private")
async def cmd_my_data(message: Message, pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    await message.answer("📦 Собираю ваши данные…")
    try:
        blob, counts = await owner_data.export_owner_data(pool, uid)
    except Exception:
        log.exception("my_data: выгрузка упала owner=%s", uid)
        await message.answer("❌ Не получилось собрать выгрузку. Попробуйте позже.",
                             reply_markup=terminal_kb())
        return
    total = sum(counts.values())
    await message.answer_document(
        BufferedInputFile(blob, filename=f"infragram_my_data_{uid}.zip"),
        caption=(f"Ваши данные: {len(counts)} разделов, {total} записей.\n"
                 "Строки сессий, токены и прокси в файл не включены."),
        reply_markup=terminal_kb())


@router.message(Command("delete_my_data"), F.chat.type == "private")
async def cmd_delete_my_data(message: Message, command: CommandObject,
                             pool: asyncpg.Pool) -> None:
    uid = message.from_user.id
    if (command.args or "").strip() != CONFIRM_WORD:
        await message.answer(
            "⚠️ <b>Удаление всех ваших данных</b>\n\n"
            "Будут удалены аккаунты и их сессии, каналы, боты, прокси, аудитории, "
            "контакты, рассылки, сценарии и история операций. Вернуть их будет "
            "нельзя.\n\n"
            "Останутся только платежи, подписки и журнал админ-действий: их "
            "хранения требует учёт. Имя в профиле будет стёрто.\n\n"
            "Сначала можно забрать копию: /my_data\n\n"
            f"Чтобы удалить, отправьте: <code>/delete_my_data {CONFIRM_WORD}</code>",
            parse_mode="HTML", reply_markup=terminal_kb())
        return
    try:
        res = await owner_data.delete_owner_data(pool, uid)
    except owner_data.OwnerDataDeleteError as e:
        log.error("delete_my_data: не удалено owner=%s: %s", uid, e)
        await message.answer(
            "❌ Удалить не получилось, ничего не изменено. Мы уже видим ошибку; "
            "напишите в поддержку, если повторная попытка не поможет.",
            reply_markup=terminal_kb())
        return
    except Exception:
        log.exception("delete_my_data: упало owner=%s", uid)
        await message.answer("❌ Удалить не получилось, ничего не изменено.",
                             reply_markup=terminal_kb())
        return
    rows = sum(res["deleted"].values())
    await message.answer(f"✅ Удалено записей: {rows}. Ваши данные стёрты.",
                         reply_markup=terminal_kb())

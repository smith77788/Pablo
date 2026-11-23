"""Заявки на вступление в каналы и чаты владельца.

Главный бот, админ канала/группы, получает `chat_join_request` и сообщает о нём
владельцу (кто добавил бота в «Модератор чатов» или чьи аккаунты ведут канал).
Заявки подключённых ботов разбирает `services/auto_responder.py`, а кнопки
«Одобрить / Отклонить» под любым таким уведомлением обрабатываются здесь:
для подключённого бота действие выполняется ЕГО токеном.
"""
from __future__ import annotations

import logging

import aiohttp
import asyncpg
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, ChatJoinRequest

from bot.callbacks import JoinReqCb
from services import bot_event_notify as ben
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()


def _cjr_dict(event: ChatJoinRequest) -> dict:
    """Апдейт aiogram → тот же вид, что приходит подключённым ботам (dict)."""
    return event.model_dump(mode="json", by_alias=True, exclude_none=True)


@router.chat_join_request()
async def on_join_request(event: ChatJoinRequest, bot: Bot, pool: asyncpg.Pool) -> None:
    try:
        owners = await ben.owners_of_chat(pool, event.chat.id)
    except Exception:
        log_exc_swallow(log, "join_requests: владелец чата")
        return
    if not owners:
        log.info("join_requests: заявка в chat=%s, владелец чата не известен",
                  event.chat.id)
        return
    cjr = _cjr_dict(event)
    for owner_id in owners:
        try:
            await ben.notify_join_request(pool, bot, owner_id, bot_id=0, cjr=cjr)
        except Exception:
            log_exc_swallow(log, "join_requests: уведомление")


async def _may_manage_main_chat(bot: Bot, pool: asyncpg.Pool, chat_id: int,
                                user_id: int) -> bool:
    if user_id in await ben.owners_of_chat(pool, chat_id):
        return True
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return getattr(m, "status", "") in {"creator", "administrator"}
    except Exception:
        return False


@router.callback_query(JoinReqCb.filter(F.action.in_({"a", "d"})))
async def cb_join_request(callback: CallbackQuery, callback_data: JoinReqCb,
                          bot: Bot, pool: asyncpg.Pool,
                          http: aiohttp.ClientSession) -> None:
    approve = callback_data.action == "a"
    chat_id, user_id = int(callback_data.chat_id), int(callback_data.user_id)
    caller = callback.from_user.id
    err = ""
    if callback_data.bot_id:
        # Подключённый бот: действовать может только его владелец, и токеном
        # именно этого бота (главный бот в том чате может и не состоять).
        row = await pool.fetchrow(
            "SELECT token FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
            int(callback_data.bot_id), caller)
        if not row:
            await callback.answer("⛔️ Это не ваш бот.", show_alert=True)
            return
        from services import bot_api
        from services.token_vault import decrypt_token

        method = "approveChatJoinRequest" if approve else "declineChatJoinRequest"
        try:
            data = await bot_api._call(http, decrypt_token(row["token"] or ""),
                                       method, chat_id=chat_id, user_id=user_id)
        except Exception as e:
            data = {"ok": False, "description": str(e)}
        if not data.get("ok"):
            err = ben.join_error_ru(str(data.get("description") or ""))
    else:
        if not await _may_manage_main_chat(bot, pool, chat_id, caller):
            await callback.answer("⛔️ Только для админов этого чата.", show_alert=True)
            return
        try:
            if approve:
                await bot.approve_chat_join_request(chat_id, user_id)
            else:
                await bot.decline_chat_join_request(chat_id, user_id)
        except Exception as e:
            err = ben.join_error_ru(str(e))
    if err:
        await callback.answer("⚠️ " + err, show_alert=True)
        return
    verdict = "✅ Одобрено" if approve else "✖️ Отклонено"
    await callback.answer(verdict)
    try:
        await callback.message.edit_text(
            (callback.message.html_text or "") + f"\n\n<b>{verdict}</b>",
            parse_mode="HTML", reply_markup=None)
    except Exception:
        log_exc_swallow(log, "join_requests: отметка в уведомлении")

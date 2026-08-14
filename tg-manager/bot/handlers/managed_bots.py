"""Manager Mode — создание дочерних ботов «в один тап» (Telegram Managed Bots).

Вместо «BotFather → /newbot → скопируй токен → вставь в нас» оператор задаёт имя
и @username, жмёт кнопку — Telegram создаёт бота, а мы автоматически получаем его
токен (getManagedBotToken) и подключаем в базу. Требует разово включённого у
@BotFather «Bot Management Mode» для нашего бота.
"""
from __future__ import annotations

import html
import logging

import aiohttp
import asyncpg
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ManagedBotUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ManagedBotCb, BotFactCb
from bot.utils.op_helpers import safe_answer
from services import managed_bots as mb
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()


class ManagedBotFSM(StatesGroup):
    wait_name = State()      # отображаемое имя будущего бота
    wait_username = State()  # желаемый @username (…bot)


def _menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🪄 Создать бота в 1 тап", callback_data=ManagedBotCb(action="new"))
    kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


_MENU_TEXT = (
    "🪄 <b>Manager Mode — боты в один тап</b>\n\n"
    "Создаёт нового бота прямо отсюда и <b>сам подключает</b> его в вашу базу — "
    "без ручного копирования токена у @BotFather.\n\n"
    "Как это работает:\n"
    "1️⃣ Задаёте имя и желаемый @username.\n"
    "2️⃣ Жмёте кнопку — Telegram открывает форму создания бота.\n"
    "3️⃣ Подтверждаете — бот автоматически появляется в списке ботов.\n\n"
    "⚠️ <b>Разовая настройка:</b> в @BotFather (его мини-апп) включите для нашего "
    "бота <b>Bot Management Mode</b> — иначе Telegram не отдаст токен нового бота."
)


@router.callback_query(ManagedBotCb.filter(F.action == "menu"))
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    try:
        await callback.message.edit_text(_MENU_TEXT, parse_mode="HTML",
                                         reply_markup=_menu_kb())
    except Exception:
        log_exc_swallow(log, "managed_bots: menu")


@router.callback_query(ManagedBotCb.filter(F.action == "cancel"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb_menu(callback, state)


@router.callback_query(ManagedBotCb.filter(F.action == "new"))
async def cb_new(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(ManagedBotFSM.wait_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ManagedBotCb(action="cancel"))
    await callback.message.edit_text(
        "🪄 <b>Новый бот — шаг 1/2</b>\n\n"
        "Пришлите <b>отображаемое имя</b> будущего бота (как его будут видеть "
        "пользователи), например: <code>Мой Помощник</code>.",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(ManagedBotFSM.wait_name, F.text)
async def msg_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("⚠️ Имя должно быть 1–64 символа. Пришлите ещё раз:")
        return
    await state.update_data(mb_name=name)
    await state.set_state(ManagedBotFSM.wait_username)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ManagedBotCb(action="cancel"))
    await message.answer(
        "🪄 <b>Новый бот — шаг 2/2</b>\n\n"
        "Пришлите желаемый <b>@username</b> бота. Правила Telegram: 5–32 символа, "
        "латиница/цифры/«_», обязательно заканчивается на <b>bot</b>.\n"
        "Например: <code>my_helper_bot</code>.\n\n"
        "<i>Если username занят — Telegram даст поправить его на шаге подтверждения.</i>",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.message(ManagedBotFSM.wait_username, F.text)
async def msg_username(message: Message, state: FSMContext, bot: Bot) -> None:
    ok, norm = mb.validate_bot_username(message.text or "")
    if not ok:
        await message.answer(f"⚠️ {norm}. Пришлите корректный @username:")
        return
    data = await state.get_data()
    name = data.get("mb_name", "")
    await state.clear()
    me = await bot.get_me()
    try:
        link = mb.build_creation_link(me.username or "", norm, name)
    except ValueError as e:
        await message.answer(f"⚠️ Не удалось собрать ссылку: {html.escape(str(e))}")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🪄 Создать бота в Telegram", url=link)
    kb.button(text="◀️ В меню", callback_data=ManagedBotCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "✅ <b>Всё готово — остался один тап</b>\n\n"
        f"Имя: <b>{html.escape(name)}</b>\n"
        f"Username: <code>@{html.escape(norm)}</code>\n\n"
        "Нажмите кнопку ниже → в Telegram откроется форма создания бота "
        "(поля можно поправить) → подтвердите. Как только бот создастся, я "
        "<b>сам подключу</b> его и пришлю подтверждение сюда.",
        parse_mode="HTML", reply_markup=kb.as_markup(), disable_web_page_preview=True)


# ── Приём созданного бота (апдейт managed_bot от Telegram) ────────────────────

@router.managed_bot()
async def on_managed_bot(event: ManagedBotUpdated, bot: Bot, pool: asyncpg.Pool,
                         http: aiohttp.ClientSession) -> None:
    """Telegram сообщил, что пользователь создал бота через нашу ссылку —
    получаем токен и автоматически подключаем бота."""
    creator = event.user
    child = event.bot_user
    creator_id = int(creator.id)
    child_uname = child.username or (child.first_name or "")

    # 1) Получить токен нового бота через Managed API.
    try:
        token = await bot.get_managed_bot_token(creator_id)
    except Exception as e:
        log.warning("managed_bots: get_managed_bot_token упал creator=%s: %s",
                    creator_id, e)
        try:
            await bot.send_message(
                creator_id,
                "⚠️ Бот создан, но я не смог получить его токен. Обычно это "
                "значит, что для меня не включён <b>Bot Management Mode</b> у "
                "@BotFather. Включите его в мини-аппе BotFather и повторите — "
                "или подключите бота вручную по токену.",
                parse_mode="HTML")
        except Exception:
            log_exc_swallow(log, "managed_bots: notify token error")
        return

    # 2) Валидировать и подключить в базу.
    res = await mb.store_managed_bot(pool, http, token, creator_id,
                                     expected_bot_id=int(child.id))
    if not res.get("ok"):
        reason = {"invalid_token": "токен недействителен",
                  "id_mismatch": "id токена не совпал с созданным ботом"}.get(
                      res.get("reason"), "неизвестная ошибка")
        try:
            await bot.send_message(
                creator_id, f"⚠️ Не удалось подключить бота: {reason}.")
        except Exception:
            log_exc_swallow(log, "managed_bots: notify store error")
        return

    # 3) Уведомить оператора об итоге.
    uname = res.get("username") or child_uname
    added = res.get("added")
    if added is True:
        text = (f"✅ <b>Бот @{html.escape(uname)} создан и подключён!</b>\n\n"
                "Он уже в вашем списке ботов — можно настраивать команды, "
                "запускать рассылки и всё остальное.")
    elif added == "taken":
        text = (f"🔒 Бот @{html.escape(uname)} уже управляется другим аккаунтом "
                "Infragram. Токен получен, но подключить нельзя.")
    else:  # False — уже был у этого оператора
        text = f"ℹ️ Бот @{html.escape(uname)} уже был в вашем списке — токен обновлён."
    kb = InlineKeyboardBuilder()
    kb.button(text="🪄 Создать ещё одного", callback_data=ManagedBotCb(action="new"))
    try:
        await bot.send_message(creator_id, text, parse_mode="HTML",
                               reply_markup=kb.as_markup())
    except Exception:
        log_exc_swallow(log, "managed_bots: notify success")
    log.info("managed_bots: подключён бот=%s creator=%s added=%s",
             res.get("bot_id"), creator_id, added)

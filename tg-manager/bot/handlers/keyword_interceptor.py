"""UI перехватчика ключей из внешних чатов (лидогенерация).

Аккаунт-читатель поллит целевой чат/канал на ключевые слова; совпадения (лиды)
пишутся в БД и пересылаются оператору фоновым циклом services/keyword_watcher.run.
Здесь — управление watcher'ами: создать / список / пауза / удалить / лиды.
"""
from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import KwCb, BmCb
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
from bot.utils.op_helpers import safe_answer
from services.logger import log_exc_swallow
from services import keyword_watcher as kw

log = logging.getLogger(__name__)
router = Router()


class KwFSM(StatesGroup):
    wait_chat = State()
    wait_keywords = State()


def _back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=KwCb(action="menu"))
    return kb


async def _render_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    watchers = await kw.list_watchers(pool, callback.from_user.id)
    lines = [
        "🎯 <b>Перехватчик лидов</b>\n",
        "Аккаунт следит за ключевыми словами во внешнем чате/канале и присылает "
        "совпадения (лиды) сюда.\n",
    ]
    kb = InlineKeyboardBuilder()
    if watchers:
        for w in watchers[:15]:
            icon = "🟢" if w["status"] == "active" else "⏸"
            kb.button(
                text=f"{icon} {w['chat_ref'][:20]} ({w['hits_count']} лид.)",
                callback_data=KwCb(action="view", watcher_id=w["id"]),
            )
    else:
        lines.append("<i>Пока нет ни одного перехватчика.</i>")
    kb.button(text="➕ Новый перехватчик", callback_data=KwCb(action="new"))
    kb.button(text="📥 Последние лиды", callback_data=KwCb(action="leads"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(KwCb.filter(F.action == "menu"))
async def cb_kw_menu(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.message.edit_text(
            locked_text("Перехватчик лидов", "pro"), parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro", back_callback=BmCb(action="monitoring")))
        return
    await _render_menu(callback, pool)


# ── Создание ─────────────────────────────────────────────────────────────────

@router.callback_query(KwCb.filter(F.action == "new"))
async def cb_kw_new(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await safe_answer(callback)
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer("Нужен тариф Pro", show_alert=True)
        return
    # Выбор аккаунта-читателя: активные аккаунты владельца.
    accs = await pool.fetch(
        "SELECT id, phone, first_name FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE AND session_str IS NOT NULL "
        "ORDER BY id LIMIT 30", callback.from_user.id)
    if not accs:
        await callback.answer("Нет активных аккаунтов. Добавьте аккаунт сначала.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for a in accs:
        label = a["first_name"] or a["phone"] or f"acc#{a['id']}"
        kb.button(text=f"👤 {label[:24]}", callback_data=KwCb(action="pick_acc", account_id=a["id"]))
    kb.button(text="◀️ Назад", callback_data=KwCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🎯 <b>Новый перехватчик</b>\n\nВыберите аккаунт-читатель "
        "<i>(он должен состоять в целевом чате)</i>:",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(KwCb.filter(F.action == "pick_acc"))
async def cb_kw_pick_acc(
    callback: CallbackQuery, callback_data: KwCb, state: FSMContext
) -> None:
    await safe_answer(callback)
    await state.update_data(account_id=callback_data.account_id)
    await state.set_state(KwFSM.wait_chat)
    await callback.message.edit_text(
        "🎯 <b>Целевой чат</b>\n\nПришлите @username, ссылку или ID чата/канала, "
        "который нужно слушать. Аккаунт должен быть его участником.",
        parse_mode="HTML", reply_markup=_back_kb().as_markup())


@router.message(KwFSM.wait_chat, F.text)
async def msg_kw_chat(message: Message, state: FSMContext) -> None:
    chat_ref = (message.text or "").strip()
    if not chat_ref or len(chat_ref) > 200:
        await message.answer("Некорректный чат. Пришлите @username, ссылку или ID.")
        return
    await state.update_data(chat_ref=chat_ref)
    await state.set_state(KwFSM.wait_keywords)
    await message.answer(
        "🔑 <b>Ключевые слова</b>\n\nПришлите ключи через запятую "
        "<i>(например: куплю, ищу, нужен мастер)</i>. Совпадение — по вхождению, "
        "регистр не важен.", parse_mode="HTML")


@router.message(KwFSM.wait_keywords, F.text)
async def msg_kw_keywords(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    keywords = kw.normalize_keywords(message.text or "")
    if not keywords:
        await message.answer("Не распознал ключи. Пришлите слова через запятую.")
        return
    data = await state.get_data()
    try:
        wid = await kw.create_watcher(
            pool, message.from_user.id, int(data["account_id"]),
            data["chat_ref"], keywords)
    except ValueError as e:
        await message.answer(f"⚠️ {e}")
        return
    except Exception:
        log_exc_swallow(log, "kw create_watcher failed")
        await message.answer("Ошибка создания перехватчика.")
        return
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 К перехватчикам", callback_data=KwCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Перехватчик #{wid} создан</b>\n\n"
        f"💬 Чат: <code>{html.escape(data['chat_ref'])}</code>\n"
        f"🔑 Ключи: {', '.join(html.escape(k) for k in keywords)}\n\n"
        "Лиды будут приходить сюда автоматически.",
        parse_mode="HTML", reply_markup=kb.as_markup())


# ── Просмотр / управление ────────────────────────────────────────────────────

@router.callback_query(KwCb.filter(F.action == "view"))
async def cb_kw_view(
    callback: CallbackQuery, callback_data: KwCb, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    w = await pool.fetchrow(
        "SELECT * FROM keyword_watchers WHERE id=$1 AND owner_id=$2",
        callback_data.watcher_id, callback.from_user.id)
    if not w:
        await callback.answer("Перехватчик не найден", show_alert=True)
        return
    import json as _json
    kws = w["keywords"]
    if isinstance(kws, str):
        kws = _json.loads(kws or "[]")
    status = "🟢 Активен" if w["status"] == "active" else "⏸ На паузе"
    kb = InlineKeyboardBuilder()
    toggle = "paused" if w["status"] == "active" else "active"
    kb.button(text="⏸ Пауза" if w["status"] == "active" else "▶️ Возобновить",
              callback_data=KwCb(action="toggle", watcher_id=w["id"]))
    kb.button(text="🗑 Удалить", callback_data=KwCb(action="del", watcher_id=w["id"]))
    kb.button(text="◀️ Назад", callback_data=KwCb(action="menu"))
    kb.adjust(2, 1)
    _ = toggle
    await callback.message.edit_text(
        f"🎯 <b>Перехватчик #{w['id']}</b>\n\n"
        f"💬 Чат: <code>{html.escape(w['chat_ref'])}</code>\n"
        f"🔑 Ключи: {', '.join(html.escape(str(k)) for k in kws)}\n"
        f"📊 Поймано лидов: <b>{w['hits_count']}</b>\n"
        f"Статус: {status}",
        parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(KwCb.filter(F.action == "toggle"))
async def cb_kw_toggle(
    callback: CallbackQuery, callback_data: KwCb, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    w = await pool.fetchrow(
        "SELECT status FROM keyword_watchers WHERE id=$1 AND owner_id=$2",
        callback_data.watcher_id, callback.from_user.id)
    if not w:
        await callback.answer("Не найден", show_alert=True)
        return
    new_status = "paused" if w["status"] == "active" else "active"
    await kw.set_status(pool, callback.from_user.id, callback_data.watcher_id, new_status)
    callback_data.action = "view"
    await cb_kw_view(callback, callback_data, pool)


@router.callback_query(KwCb.filter(F.action == "del"))
async def cb_kw_del(
    callback: CallbackQuery, callback_data: KwCb, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    await kw.delete_watcher(pool, callback.from_user.id, callback_data.watcher_id)
    await callback.answer("Удалён")
    await _render_menu(callback, pool)


@router.callback_query(KwCb.filter(F.action == "leads"))
async def cb_kw_leads(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    hits = await kw.list_hits(pool, callback.from_user.id, limit=15)
    if not hits:
        await callback.message.edit_text(
            "📥 <b>Лиды</b>\n\nПока пусто. Как только в отслеживаемых чатах появятся "
            "сообщения по ключам — они придут сюда.",
            parse_mode="HTML", reply_markup=_back_kb().as_markup())
        return
    lines = ["📥 <b>Последние лиды</b>\n"]
    for h in hits:
        who = f"@{h['from_username']}" if h["from_username"] else f"id{h['from_user_id'] or '?'}"
        txt = html.escape((h["text"] or "")[:120])
        lines.append(f"🔑 <b>{html.escape(h['matched_keyword'] or '')}</b> · {who}\n{txt}\n")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=_back_kb().as_markup())

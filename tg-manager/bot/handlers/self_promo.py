"""Self-promotion module — Infragram рекламирует себя через управляемые каналы и DM.

Функции:
  1. Библиотека шаблонов (прямая и нативная реклама) — CRUD (только admin)
  2. Публикация шаблона во все управляемые каналы пользователя
  3. Переход к DM-кампании с предзаполненным текстом
  4. Реферальная ссылка — готовый текст для репоста
  5. История запусков
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import os

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, DmCb, SelfPromoCb
from database import db
from services import account_manager

log = logging.getLogger(__name__)
router = Router()

_PAGE = 5  # шаблонов на страницу


def _is_admin(uid: int) -> bool:
    raw = os.getenv("ADMIN_IDS", "")
    return uid in {int(x.strip()) for x in raw.split(",") if x.strip().isdigit()}


# ─── FSM ──────────────────────────────────────────────────────────────────────

class SelfPromoFSM(StatesGroup):
    add_style    = State()
    add_title    = State()
    add_content  = State()
    add_cta_text = State()
    add_cta_url  = State()


# ─── Keyboards ────────────────────────────────────────────────────────────────

def _menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Шаблоны контента", callback_data=SelfPromoCb(action="list", page=0))
    kb.button(text="🚀 Запустить в каналы", callback_data=SelfPromoCb(action="launch_channel"))
    kb.button(text="🔗 Реферальная ссылка", callback_data=SelfPromoCb(action="share_link"))
    kb.button(text="📊 История запусков", callback_data=SelfPromoCb(action="history", page=0))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return kb.as_markup()


def _list_kb(rows, page: int, total: int, is_adm: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for r in rows:
        badge = "🎯" if r["style"] == "direct" else "💡"
        kb.button(
            text=f"{badge} {r['title'][:38]}",
            callback_data=SelfPromoCb(action="view", item_id=r["id"]),
        )
    nav = []
    if page > 0:
        nav.append(("◀️", SelfPromoCb(action="list", page=page - 1)))
    if (page + 1) * _PAGE < total:
        nav.append(("▶️", SelfPromoCb(action="list", page=page + 1)))
    for label, cd in nav:
        kb.button(text=label, callback_data=cd)
    if is_adm:
        kb.button(text="➕ Добавить шаблон", callback_data=SelfPromoCb(action="add_ask"))
    kb.button(text="◀️ Меню", callback_data=SelfPromoCb(action="menu"))
    row_sizes = [1] * len(rows) + ([len(nav)] if nav else []) + [1] * (1 if is_adm else 0) + [1]
    kb.adjust(*row_sizes)
    return kb.as_markup()


def _view_kb(tpl_id: int, is_adm: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Опубликовать в каналы", callback_data=SelfPromoCb(action="run_confirm", item_id=tpl_id))
    kb.button(text="📨 Создать DM-кампанию", callback_data=DmCb(action="menu"))
    if is_adm:
        kb.button(text="🗑 Удалить шаблон", callback_data=SelfPromoCb(action="del_confirm", item_id=tpl_id))
    kb.button(text="◀️ К шаблонам", callback_data=SelfPromoCb(action="list", page=0))
    kb.adjust(1)
    return kb.as_markup()


def _run_confirm_kb(tpl_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=SelfPromoCb(action="run_now", item_id=tpl_id))
    kb.button(text="◀️ Отмена", callback_data=SelfPromoCb(action="view", item_id=tpl_id))
    kb.adjust(2)
    return kb.as_markup()


def _del_confirm_kb(tpl_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=SelfPromoCb(action="del_do", item_id=tpl_id))
    kb.button(text="◀️ Отмена", callback_data=SelfPromoCb(action="view", item_id=tpl_id))
    kb.adjust(2)
    return kb.as_markup()


def _style_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 Прямая реклама", callback_data=SelfPromoCb(action="add_style", style="direct"))
    kb.button(text="💡 Нативная реклама", callback_data=SelfPromoCb(action="add_style", style="native"))
    kb.button(text="◀️ Отмена", callback_data=SelfPromoCb(action="list", page=0))
    kb.adjust(2, 1)
    return kb.as_markup()


def _cancel_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data=SelfPromoCb(action="list", page=0))
    kb.adjust(1)
    return kb.as_markup()


def _skip_back_kb(skip_action: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=SelfPromoCb(action=skip_action))
    kb.button(text="◀️ Отмена", callback_data=SelfPromoCb(action="list", page=0))
    kb.adjust(2)
    return kb.as_markup()


def _back_to_list_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 К шаблонам", callback_data=SelfPromoCb(action="list", page=0))
    kb.adjust(1)
    return kb.as_markup()


# ─── Command / entry ──────────────────────────────────────────────────────────

@router.message(Command("self_promo"))
async def cmd_self_promo(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (
        "🎯 <b>Самопиар & Реклама</b>\n\n"
        "Система для продвижения Infragram через управляемые каналы и аудиторию.\n\n"
        "• <b>Прямая реклама</b> — явные рекламные посты\n"
        "• <b>Нативная реклама</b> — полезный контент с упоминанием\n"
        "• <b>Реферальная ссылка</b> — готовый текст для репоста"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_menu_kb())


@router.callback_query(SelfPromoCb.filter(F.action == "menu"))
async def cb_sp_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    text = (
        "🎯 <b>Самопиар & Реклама</b>\n\n"
        "Система для продвижения Infragram через управляемые каналы и аудиторию.\n\n"
        "• <b>Прямая реклама</b> — явные рекламные посты\n"
        "• <b>Нативная реклама</b> — полезный контент с упоминанием\n"
        "• <b>Реферальная ссылка</b> — готовый текст для репоста"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_menu_kb())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=_menu_kb())


# ─── Template list ─────────────────────────────────────────────────────────────

@router.callback_query(SelfPromoCb.filter(F.action == "list"))
async def cb_sp_list(callback: CallbackQuery, callback_data: SelfPromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    page = callback_data.page
    all_rows = await pool.fetch(
        "SELECT id, style, title, use_count FROM self_promo_templates WHERE is_active ORDER BY id",
    )
    total = len(all_rows)
    page_rows = all_rows[page * _PAGE: (page + 1) * _PAGE]
    is_adm = _is_admin(callback.from_user.id)
    badge_map = {"direct": "🎯 Прямая", "native": "💡 Нативная"}
    lines = [
        f"• [{badge_map.get(r['style'], r['style'])}] {_html.escape(r['title'])}"
        for r in page_rows
    ]
    body = "\n".join(lines) if lines else "Нет шаблонов. Нажмите ➕ для добавления."
    text = f"📝 <b>Шаблоны контента</b> ({total} шт.)\n\n{body}"
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_list_kb(page_rows, page, total, is_adm)
        )
    except Exception:
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=_list_kb(page_rows, page, total, is_adm)
        )


# ─── Template view ─────────────────────────────────────────────────────────────

@router.callback_query(SelfPromoCb.filter(F.action == "view"))
async def cb_sp_view(callback: CallbackQuery, callback_data: SelfPromoCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    tpl = await pool.fetchrow("SELECT * FROM self_promo_templates WHERE id=$1", callback_data.item_id)
    if not tpl:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    badge = "🎯 Прямая реклама" if tpl["style"] == "direct" else "💡 Нативная реклама"
    cta_line = ""
    if tpl["cta_text"]:
        cta_line = f"\n\n<b>CTA-кнопка:</b> {_html.escape(tpl['cta_text'])}"
        if tpl["cta_url"]:
            cta_line += f"\n{tpl['cta_url']}"
    ref_note = "\n<i>+ реферальная ссылка добавляется автоматически</i>" if tpl["add_referral"] else ""
    text = (
        f"<b>{_html.escape(tpl['title'])}</b> [{badge}]\n"
        f"Использований: {tpl['use_count']}\n"
        f"{'─' * 28}\n\n"
        f"{tpl['content']}"
        f"{cta_line}{ref_note}"
    )
    is_adm = _is_admin(callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_view_kb(tpl["id"], is_adm))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=_view_kb(tpl["id"], is_adm))


# ─── Delete template ──────────────────────────────────────────────────────────

@router.callback_query(SelfPromoCb.filter(F.action == "del_confirm"))
async def cb_sp_del_confirm(callback: CallbackQuery, callback_data: SelfPromoCb, pool: asyncpg.Pool) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await callback.answer()
    tpl = await pool.fetchrow("SELECT title FROM self_promo_templates WHERE id=$1", callback_data.item_id)
    if not tpl:
        await callback.answer("Не найден", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            f"🗑 Удалить шаблон?\n\n<b>{_html.escape(tpl['title'])}</b>",
            parse_mode="HTML",
            reply_markup=_del_confirm_kb(callback_data.item_id),
        )
    except Exception:
        pass


@router.callback_query(SelfPromoCb.filter(F.action == "del_do"))
async def cb_sp_del_do(callback: CallbackQuery, callback_data: SelfPromoCb, pool: asyncpg.Pool) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await pool.execute(
        "UPDATE self_promo_templates SET is_active=FALSE WHERE id=$1", callback_data.item_id
    )
    await callback.answer("✅ Удалено")
    all_rows = await pool.fetch(
        "SELECT id, style, title, use_count FROM self_promo_templates WHERE is_active ORDER BY id"
    )
    page_rows = all_rows[:_PAGE]
    text = f"📝 <b>Шаблоны контента</b> ({len(all_rows)} шт.)"
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_list_kb(page_rows, 0, len(all_rows), True)
        )
    except Exception:
        pass


# ─── Add template FSM ─────────────────────────────────────────────────────────

@router.callback_query(SelfPromoCb.filter(F.action == "add_ask"))
async def cb_sp_add_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        await callback.answer("Только для администраторов", show_alert=True)
        return
    await callback.answer()
    await state.set_state(SelfPromoFSM.add_style)
    try:
        await callback.message.edit_text(
            "➕ <b>Новый шаблон</b>\n\nВыберите тип рекламы:",
            parse_mode="HTML",
            reply_markup=_style_kb(),
        )
    except Exception:
        await callback.message.answer(
            "➕ <b>Новый шаблон</b>\n\nВыберите тип рекламы:",
            parse_mode="HTML",
            reply_markup=_style_kb(),
        )


@router.callback_query(SelfPromoCb.filter(F.action == "add_style"), SelfPromoFSM.add_style)
async def cb_sp_add_style(callback: CallbackQuery, callback_data: SelfPromoCb, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(style=callback_data.style)
    await state.set_state(SelfPromoFSM.add_title)
    badge = "🎯 Прямая" if callback_data.style == "direct" else "💡 Нативная"
    try:
        await callback.message.edit_text(
            f"Тип: <b>{badge}</b>\n\nВведите <b>название</b> шаблона (до 100 символов):",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
    except Exception:
        await callback.message.answer(
            f"Тип: <b>{badge}</b>\n\nВведите <b>название</b> шаблона (до 100 символов):",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )


@router.message(SelfPromoFSM.add_title)
async def fsm_sp_add_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()[:100]
    if not title:
        await message.answer("Название не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(title=title)
    await state.set_state(SelfPromoFSM.add_content)
    await message.answer(
        "Введите <b>текст</b> рекламного поста (HTML-форматирование поддерживается):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(SelfPromoFSM.add_content)
async def fsm_sp_add_content(message: Message, state: FSMContext) -> None:
    content = (message.text or "").strip()
    if not content:
        await message.answer("Текст не может быть пустым. Введите ещё раз:")
        return
    await state.update_data(content=content)
    await state.set_state(SelfPromoFSM.add_cta_text)
    await message.answer(
        "Введите <b>текст CTA-кнопки</b> (например: «🤖 Попробовать Infragram»)\nИли пропустите:",
        parse_mode="HTML",
        reply_markup=_skip_back_kb("add_skip_cta"),
    )


@router.callback_query(SelfPromoCb.filter(F.action == "add_skip_cta"), SelfPromoFSM.add_cta_text)
async def cb_sp_skip_cta(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    data = await state.get_data()
    await _save_template(pool, data, cta_text=None, cta_url=None)
    await state.clear()
    try:
        await callback.message.edit_text(
            "✅ <b>Шаблон сохранён!</b>", parse_mode="HTML", reply_markup=_back_to_list_kb()
        )
    except Exception:
        pass


@router.message(SelfPromoFSM.add_cta_text)
async def fsm_sp_add_cta_text(message: Message, state: FSMContext) -> None:
    cta_text = (message.text or "").strip()[:100]
    await state.update_data(cta_text=cta_text)
    await state.set_state(SelfPromoFSM.add_cta_url)
    await message.answer(
        "Введите <b>URL для кнопки</b> (например: https://t.me/InfragramBot):",
        parse_mode="HTML",
        reply_markup=_skip_back_kb("add_skip_url"),
    )


@router.callback_query(SelfPromoCb.filter(F.action == "add_skip_url"), SelfPromoFSM.add_cta_url)
async def cb_sp_skip_url(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    data = await state.get_data()
    await _save_template(pool, data, cta_text=data.get("cta_text"), cta_url=None)
    await state.clear()
    try:
        await callback.message.edit_text(
            "✅ <b>Шаблон сохранён!</b>", parse_mode="HTML", reply_markup=_back_to_list_kb()
        )
    except Exception:
        pass


@router.message(SelfPromoFSM.add_cta_url)
async def fsm_sp_add_cta_url(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    cta_url = (message.text or "").strip()
    data = await state.get_data()
    await _save_template(pool, data, cta_text=data.get("cta_text"), cta_url=cta_url)
    await state.clear()
    await message.answer("✅ <b>Шаблон сохранён!</b>", parse_mode="HTML", reply_markup=_back_to_list_kb())


async def _save_template(pool: asyncpg.Pool, data: dict, cta_text, cta_url) -> None:
    await pool.execute(
        """INSERT INTO self_promo_templates(style, title, content, cta_text, cta_url, add_referral)
           VALUES($1,$2,$3,$4,$5,$6)""",
        data.get("style", "direct"),
        data.get("title", "Без названия"),
        data.get("content", ""),
        cta_text,
        cta_url,
        True,
    )


# ─── Launch: select template → all channels ───────────────────────────────────

@router.callback_query(SelfPromoCb.filter(F.action == "launch_channel"))
async def cb_sp_launch_channel(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    rows = await pool.fetch(
        "SELECT id, style, title FROM self_promo_templates WHERE is_active ORDER BY id LIMIT 10"
    )
    if not rows:
        await callback.answer("Нет шаблонов.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for r in rows:
        badge = "🎯" if r["style"] == "direct" else "💡"
        kb.button(
            text=f"{badge} {r['title'][:38]}",
            callback_data=SelfPromoCb(action="run_confirm", item_id=r["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=SelfPromoCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            "🚀 <b>Выберите шаблон для публикации:</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await callback.message.answer(
            "🚀 <b>Выберите шаблон для публикации:</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


@router.callback_query(SelfPromoCb.filter(F.action == "run_confirm"))
async def cb_sp_run_confirm(
    callback: CallbackQuery, callback_data: SelfPromoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    tpl = await pool.fetchrow("SELECT * FROM self_promo_templates WHERE id=$1", callback_data.item_id)
    if not tpl:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    channels = await db.get_managed_channels(pool, callback.from_user.id)
    if not channels:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=SelfPromoCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            "⚠️ <b>Нет управляемых каналов</b>\n\n"
            "Добавьте каналы через раздел «📡 Каналы».",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    badge = "🎯 Прямая" if tpl["style"] == "direct" else "💡 Нативная"
    preview = (tpl["content"] or "")[:200] + ("…" if len(tpl["content"] or "") > 200 else "")
    ref_note = "\n+ реферальная ссылка будет добавлена" if tpl["add_referral"] else ""
    text = (
        f"🚀 <b>Подтверждение публикации</b>\n\n"
        f"Шаблон: <b>{_html.escape(tpl['title'])}</b> [{badge}]\n"
        f"Каналов: <b>{len(channels)}</b>{ref_note}\n\n"
        f"<i>Предпросмотр:</i>\n{_html.escape(preview)}"
    )
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_run_confirm_kb(tpl["id"])
        )
    except Exception:
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=_run_confirm_kb(tpl["id"])
        )


@router.callback_query(SelfPromoCb.filter(F.action == "run_now"))
async def cb_sp_run_now(
    callback: CallbackQuery, callback_data: SelfPromoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Запускаю…")
    tpl = await pool.fetchrow("SELECT * FROM self_promo_templates WHERE id=$1", callback_data.item_id)
    if not tpl:
        return
    user_id = callback.from_user.id
    channels = await db.get_managed_channels(pool, user_id)
    # Build content: append referral link if needed
    content = tpl["content"] or ""
    if tpl["add_referral"]:
        try:
            me = await callback.bot.get_me()
            code = await db.get_or_create_referral_code(pool, user_id)
            content += f"\n\n🔗 t.me/{me.username}?start={code}"
        except Exception:
            pass
    # Record the run
    run_id = await pool.fetchval(
        """INSERT INTO self_promo_runs(template_id, run_type, initiated_by)
           VALUES($1,'channel_post',$2) RETURNING id""",
        tpl["id"], user_id,
    )
    asyncio.create_task(
        _post_to_channels_bg(callback.bot, pool, user_id, run_id, int(tpl["id"]), channels, content)
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 История", callback_data=SelfPromoCb(action="history", page=0))
    kb.button(text="◀️ Меню", callback_data=SelfPromoCb(action="menu"))
    kb.adjust(2)
    try:
        await callback.message.edit_text(
            f"⏳ <b>Публикация запущена!</b>\n\n"
            f"Отправляю в <b>{len(channels)}</b> каналов (1.5 сек/канал).\n"
            f"Отчёт придёт по завершении.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass


async def _post_to_channels_bg(
    bot: Bot,
    pool: asyncpg.Pool,
    user_id: int,
    run_id: int,
    template_id: int,
    channels: list,
    content: str,
) -> None:
    sent = failed = 0
    for ch in channels:
        try:
            acc_row = await db.get_account_for_telethon(pool, ch["acc_id"], user_id)
            if not acc_row or not acc_row["session_str"]:
                failed += 1
                continue
            res = await account_manager.post_to_channel(
                session_string=acc_row["session_str"],
                channel_id=ch["channel_id"],
                text=content,
                access_hash=ch.get("access_hash") or 0,
                _acc=dict(acc_row),
            )
            if res.get("error"):
                failed += 1
            else:
                sent += 1
        except Exception as exc:
            log.warning("self_promo channel=%s: %s", ch.get("channel_id"), exc)
            failed += 1
        await asyncio.sleep(1.5)
    await pool.execute(
        "UPDATE self_promo_runs SET sent=$1, failed=$2, status='done', completed_at=NOW() WHERE id=$3",
        sent, failed, run_id,
    )
    await pool.execute(
        "UPDATE self_promo_templates SET use_count=use_count+$1 WHERE id=$2",
        sent, template_id,
    )
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Самопиар завершён!</b>\n\n"
            f"📢 Опубликовано: <b>{sent}</b>\n"
            f"⚠️ Ошибок: <b>{failed}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─── Referral link ─────────────────────────────────────────────────────────────

@router.callback_query(SelfPromoCb.filter(F.action == "share_link"))
async def cb_sp_share_link(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    try:
        me = await callback.bot.get_me()
        code = await db.get_or_create_referral_code(pool, user_id)
        link = f"https://t.me/{me.username}?start={code}"
    except Exception:
        link = "https://t.me/InfragramBot"
    ready_text = (
        f"🚀 Пользуюсь Infragram — автоматизация Telegram на новом уровне. "
        f"Управление каналами, DM-кампании, прогрев аккаунтов — всё в одном месте.\n"
        f"Попробуй бесплатно: {link}"
    )
    text = (
        f"🔗 <b>Реферальная ссылка</b>\n\n"
        f"<code>{link}</code>\n\n"
        f"<b>Готовый текст для репоста:</b>\n\n"
        f"<i>{_html.escape(ready_text)}</i>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=SelfPromoCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ─── Run history ──────────────────────────────────────────────────────────────

@router.callback_query(SelfPromoCb.filter(F.action == "history"))
async def cb_sp_history(
    callback: CallbackQuery, callback_data: SelfPromoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    page = callback_data.page
    limit = 10
    offset = page * limit
    runs = await pool.fetch(
        """SELECT r.id, r.run_type, r.sent, r.failed, r.status, r.created_at,
                  t.title AS tpl_title
           FROM self_promo_runs r
           LEFT JOIN self_promo_templates t ON t.id = r.template_id
           WHERE r.initiated_by = $1
           ORDER BY r.created_at DESC
           LIMIT $2 OFFSET $3""",
        user_id, limit, offset,
    )
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM self_promo_runs WHERE initiated_by=$1", user_id
    )
    kb = InlineKeyboardBuilder()
    if not runs:
        kb.button(text="◀️ Назад", callback_data=SelfPromoCb(action="menu"))
        kb.adjust(1)
        try:
            await callback.message.edit_text(
                "📊 <b>История запусков</b>\n\nПока нет запусков. Нажмите «Запустить в каналы».",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        except Exception:
            pass
        return
    type_map = {"channel_post": "📢", "dm_blast": "📨", "manual": "✍️"}
    lines = []
    for r in runs:
        ts = r["created_at"].strftime("%d.%m %H:%M")
        icon = type_map.get(r["run_type"], "•")
        name = (r["tpl_title"] or "?")[:22]
        stat = "⏳" if r["status"] == "running" else f"✅{r['sent']} ⚠️{r['failed']}"
        lines.append(f"• {ts} {icon} {name} → {stat}")
    text = f"📊 <b>История запусков</b> ({total})\n\n" + "\n".join(lines)
    if page > 0:
        kb.button(text="◀️", callback_data=SelfPromoCb(action="history", page=page - 1))
    if (page + 1) * limit < total:
        kb.button(text="▶️", callback_data=SelfPromoCb(action="history", page=page + 1))
    kb.button(text="◀️ Меню", callback_data=SelfPromoCb(action="menu"))
    kb.adjust(2, 1) if page > 0 or (page + 1) * limit < total else kb.adjust(1)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())

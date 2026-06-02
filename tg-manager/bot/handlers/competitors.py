"""Мониторинг конкурирующих каналов."""

import re
import asyncio
import logging
import aiohttp
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import CompCb, BmCb
from bot.states import AddCompetitorFSM
from bot.utils.op_helpers import safe_edit
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()


def _members_trend(current: int | None, previous: int | None) -> str:
    """Возвращает стрелку тренда подписчиков и дельту."""
    if current is None:
        return "?"
    base = f"{current:,}"
    if previous is None or previous == current:
        return base
    delta = current - previous
    if delta > 0:
        return f"{base} ↗️ (+{delta:,})"
    return f"{base} ↘️ ({delta:,})"


@router.callback_query(CompCb.filter(F.action == "menu"))
async def comp_menu(cb: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    # Если пользователь нажал «Назад» из FSM — сбрасываем состояние
    await state.clear()
    await cb.answer()
    try:
        rows = await pool.fetch(
            "SELECT id, username, label, last_members, prev_members, last_checked "
            "FROM competitors WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 10",
            cb.from_user.id,
        )
    except Exception:
        rows = []

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить", callback_data=CompCb(action="add"))
    kb.button(text="🔄 Обновить данные", callback_data=CompCb(action="refresh"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="analytics"))
    kb.adjust(2, 1)

    if not rows:
        lines = [
            "🏆 <b>Конкуренты</b>\n",
            "Список пуст.\n\n"
            "💡 Добавьте каналы конкурентов для мониторинга динамики подписчиков.\n"
            "Данные обновляются вручную кнопкой «🔄 Обновить данные».",
        ]
        await safe_edit(cb, "\n".join(lines), reply_markup=kb.as_markup())
        return

    lines = ["🏆 <b>Конкуренты</b>\n"]
    for r in rows:
        checked = (
            r["last_checked"].strftime("%d.%m %H:%M") if r["last_checked"] else "—"
        )
        members_str = _members_trend(r["last_members"], r["prev_members"])
        label = r["label"] or r["username"]
        lines.append(
            f"• @{r['username']} <i>({label})</i>\n"
            f"  Подписчики: <b>{members_str}</b> | обновлено: {checked}"
        )
        kb.button(
            text=f"🗑 @{r['username']}",
            callback_data=CompCb(action="delete", comp_id=r["id"]),
        )

    await safe_edit(cb, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(CompCb.filter(F.action == "add"))
async def comp_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(AddCompetitorFSM.waiting_username)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=CompCb(action="cancel_fsm"))
    await safe_edit(
        cb,
        "➕ <b>Добавить конкурента</b>\n\n"
        "Введите @username канала или бота конкурента:\n\n"
        "💡 Например: <code>@durov</code> или просто <code>durov</code>\n"
        "<i>Только username без пробелов, не ссылка.</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CompCb.filter(F.action == "cancel_fsm"))
async def comp_cancel_fsm(cb: CallbackQuery, state: FSMContext) -> None:
    """Отмена FSM из любого шага добавления конкурента."""
    await state.clear()
    await cb.answer("Отменено")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку конкурентов", callback_data=CompCb(action="menu"))
    await safe_edit(cb, "❌ Добавление конкурента отменено.", reply_markup=kb.as_markup())


@router.message(AddCompetitorFSM.waiting_username, F.text)
async def comp_got_username(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().lstrip("@").strip()

    # Валидация: непустой, только допустимые символы Telegram username
    if not raw or len(raw) < 3:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=CompCb(action="cancel_fsm"))
        await message.answer(
            "⚠️ Username слишком короткий (минимум 3 символа). Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return

    if len(raw) > 32 or not re.match(r"^[a-zA-Z0-9_]+$", raw):
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=CompCb(action="cancel_fsm"))
        await message.answer(
            "⚠️ Недопустимый username. Используйте только буквы, цифры и _\n"
            "Длина: 3–32 символа. Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return

    await state.update_data(username=raw)
    await state.set_state(AddCompetitorFSM.waiting_label)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=CompCb(action="skip_label"))
    kb.button(text="❌ Отмена", callback_data=CompCb(action="cancel_fsm"))
    kb.adjust(1)
    await message.answer(
        f"Введите метку для <b>@{raw}</b> (например: «Главный конкурент»).\n"
        "Или нажмите «Пропустить»:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CompCb.filter(F.action == "skip_label"))
async def comp_skip_label(
    cb: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await cb.answer()
    data = await state.get_data()
    await state.clear()
    username = data.get("username", "")
    if not username:
        err_kb = InlineKeyboardBuilder()
        err_kb.button(text="◀️ К списку", callback_data=CompCb(action="menu"))
        await safe_edit(
            cb,
            "⚠️ Ошибка: данные не найдены. Начните заново.",
            reply_markup=err_kb.as_markup(),
        )
        return
    await _save_competitor(cb, pool, username, None)


@router.message(AddCompetitorFSM.waiting_label, F.text)
async def comp_got_label(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    username = data.get("username", "")
    label = (message.text or "").strip()
    await state.clear()

    if not label:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ К списку", callback_data=CompCb(action="menu"))
        await message.answer(
            "⚠️ Метка не может быть пустой.", reply_markup=kb.as_markup()
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку", callback_data=CompCb(action="menu"))
    try:
        await pool.execute(
            "INSERT INTO competitors(owner_id, username, label) VALUES($1,$2,$3) "
            "ON CONFLICT(owner_id, username) DO UPDATE SET label=$3",
            message.from_user.id,
            username,
            label,
        )
        await message.answer(
            f"✅ @{username} <i>({label})</i> добавлен.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=kb.as_markup())


async def _save_competitor(
    cb: CallbackQuery, pool: asyncpg.Pool, username: str, label
) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку", callback_data=CompCb(action="menu"))
    try:
        await pool.execute(
            "INSERT INTO competitors(owner_id, username, label) VALUES($1,$2,$3) "
            "ON CONFLICT(owner_id, username) DO NOTHING",
            cb.from_user.id,
            username,
            label,
        )
        await safe_edit(
            cb,
            f"✅ @{username} добавлен.\n\n"
            "Нажмите «🔄 Обновить данные» в списке, чтобы загрузить текущее число подписчиков.",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await safe_edit(cb, f"❌ Ошибка: {e}", reply_markup=kb.as_markup())


@router.callback_query(CompCb.filter(F.action == "refresh"))
async def comp_refresh(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    await cb.answer()
    try:
        rows = await pool.fetch(
            "SELECT id, username, last_members FROM competitors WHERE owner_id=$1",
            cb.from_user.id,
        )
    except Exception:
        rows = []

    if not rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=CompCb(action="menu"))
        await safe_edit(
            cb,
            "🏆 <b>Конкуренты</b>\n\nСписок конкурентов пуст.\n\n"
            "💡 Сначала добавьте конкурентов кнопкой «➕ Добавить».",
            reply_markup=kb.as_markup(),
        )
        return

    await safe_edit(cb, f"🔄 Обновляю данные для {len(rows)} конкурентов...")

    updated = 0
    async with aiohttp.ClientSession() as sess:
        for r in rows:
            try:
                resp = await sess.get(
                    f"https://t.me/{r['username']}",
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=8),
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                text = await resp.text()
                m = re.search(r'"members_count":(\d+)', text)
                if not m:
                    m = re.search(r"([\d\s]{3,})\s*(?:subscribers|members)", text)
                if m:
                    count = int(m.group(1).replace(" ", "").replace("\xa0", ""))
                    # Сохраняем предыдущее значение перед обновлением для вычисления тренда
                    await pool.execute(
                        "UPDATE competitors "
                        "SET prev_members = last_members, "
                        "    last_members = $1, "
                        "    last_checked = now() "
                        "WHERE id = $2",
                        count,
                        r["id"],
                    )
                    updated += 1
                await asyncio.sleep(1.5)
            except Exception:
                log_exc_swallow(log, "Не удалось обновить данные конкурента")

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Смотреть список", callback_data=CompCb(action="menu"))
    await safe_edit(
        cb,
        f"✅ Обновлено {updated} из {len(rows)} конкурентов.\n\n"
        "Тренды подписчиков (↗️↘️) теперь видны в списке.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CompCb.filter(F.action == "delete"))
async def comp_delete(
    cb: CallbackQuery, callback_data: CompCb, pool: asyncpg.Pool
) -> None:
    await cb.answer()
    try:
        row = await pool.fetchrow(
            "SELECT username FROM competitors WHERE id=$1 AND owner_id=$2",
            callback_data.comp_id,
            cb.from_user.id,
        )
        if row:
            await pool.execute(
                "DELETE FROM competitors WHERE id=$1 AND owner_id=$2",
                callback_data.comp_id,
                cb.from_user.id,
            )
            username = row["username"]
            msg = f"🗑 @{username} удалён из списка конкурентов."
        else:
            msg = "⚠️ Конкурент не найден или уже удалён."
    except Exception:
        log_exc_swallow(log, "Не удалось удалить конкурента")
        msg = "❌ Ошибка при удалении. Попробуйте позже."

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку", callback_data=CompCb(action="menu"))
    await safe_edit(cb, msg, reply_markup=kb.as_markup())

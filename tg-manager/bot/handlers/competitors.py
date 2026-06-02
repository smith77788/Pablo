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


@router.callback_query(CompCb.filter(F.action == "menu"))
async def comp_menu(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    await cb.answer()
    try:
        rows = await pool.fetch(
            "SELECT id, username, label, last_members, last_checked "
            "FROM competitors WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 10",
            cb.from_user.id,
        )
    except Exception:
        rows = []

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить", callback_data=CompCb(action="add"))
    kb.button(text="🔄 Обновить", callback_data=CompCb(action="refresh"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="analytics"))
    kb.adjust(2, 1)

    lines = ["🏆 <b>Конкуренты</b>\n"]
    for r in rows:
        checked = (
            r["last_checked"].strftime("%d.%m %H:%M") if r["last_checked"] else "—"
        )
        members = f"{r['last_members']:,}" if r["last_members"] else "?"
        label = r["label"] or r["username"]
        lines.append(
            f"• @{r['username']} <i>({label})</i> — {members} подп. | {checked}"
        )
        kb.button(
            text=f"🗑 @{r['username']}",
            callback_data=CompCb(action="delete", comp_id=r["id"]),
        )

    if not rows:
        lines.append("Список пуст. Добавьте конкурентов для мониторинга.")

    await safe_edit(cb, "\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(CompCb.filter(F.action == "add"))
async def comp_add(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer()
    await state.set_state(AddCompetitorFSM.waiting_username)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=CompCb(action="menu"))
    await safe_edit(
        cb,
        "Введите @username канала конкурента:\n\n"
        "💡 Например: <code>@durov</code> или просто <code>durov</code>\n"
        "<i>Только username, не ссылка.</i>",
        reply_markup=kb.as_markup(),
    )


@router.message(AddCompetitorFSM.waiting_username, F.text)
async def comp_got_username(message: Message, state: FSMContext) -> None:
    username = message.text.strip().lstrip("@")
    await state.update_data(username=username)
    await state.set_state(AddCompetitorFSM.waiting_label)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data=CompCb(action="skip_label"))
    kb.button(text="❌ Отмена", callback_data=CompCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"Метка для @{username} (например: «Конкурент 1»). Или пропустите:",
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
    await _save_competitor(cb, pool, username, None)


@router.message(AddCompetitorFSM.waiting_label, F.text)
async def comp_got_label(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    username = data.get("username", "")
    label = message.text.strip()
    await state.clear()

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
            f"✅ @{username} ({label}) добавлен.", reply_markup=kb.as_markup()
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
        await safe_edit(cb, f"✅ @{username} добавлен.", reply_markup=kb.as_markup())
    except Exception as e:
        await safe_edit(cb, f"❌ Ошибка: {e}", reply_markup=kb.as_markup())


@router.callback_query(CompCb.filter(F.action == "refresh"))
async def comp_refresh(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    await cb.answer()
    try:
        rows = await pool.fetch(
            "SELECT id, username FROM competitors WHERE owner_id=$1", cb.from_user.id
        )
    except Exception:
        rows = []

    if not rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=CompCb(action="menu"))
        await safe_edit(cb, "Список конкурентов пуст.", reply_markup=kb.as_markup())
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
                    await pool.execute(
                        "UPDATE competitors SET last_members=$1, last_checked=now() WHERE id=$2",
                        count,
                        r["id"],
                    )
                    updated += 1
                await asyncio.sleep(1.5)
            except Exception:
                log_exc_swallow(log, "Не удалось обновить данные конкурента")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку", callback_data=CompCb(action="menu"))
    await safe_edit(
        cb,
        f"✅ Обновлено {updated} из {len(rows)} конкурентов.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CompCb.filter(F.action == "delete"))
async def comp_delete(
    cb: CallbackQuery, callback_data: CompCb, pool: asyncpg.Pool
) -> None:
    await cb.answer()
    try:
        await pool.execute(
            "DELETE FROM competitors WHERE id=$1 AND owner_id=$2",
            callback_data.comp_id,
            cb.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "Не удалось удалить конкурента")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К списку", callback_data=CompCb(action="menu"))
    await safe_edit(cb, "🗑 Конкурент удалён.", reply_markup=kb.as_markup())

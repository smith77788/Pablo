"""
Audience Parser — UI для парсинга аудитории из каналов/групп Telegram.

Flows:
- Список запусков парсера с историей
- Запуск парсинга участников / активных пользователей
- Просмотр спарсенной аудитории с фильтрами
- Экспорт в CSV / текстовый список
- Удаление аудитории

Subscription: STARTER минимум для парсинга.
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ParserCb
from bot.utils.subscription import require_plan
from database import db

log = logging.getLogger(__name__)
router = Router()

_PAGE_SIZE = 10


class ParserFSM(StatesGroup):
    waiting_source = State()
    waiting_limit   = State()


def _back_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ParserCb(action="menu"))
    return kb


def _menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Парсить участников",        callback_data=ParserCb(action="start_members"))
    kb.button(text="⚡ Парсить активных (группа)", callback_data=ParserCb(action="start_active"))
    kb.button(text="📋 История запусков",          callback_data=ParserCb(action="runs"))
    kb.button(text="📊 Моя аудитория",             callback_data=ParserCb(action="audience"))
    kb.button(text="🗑 Очистить всю аудиторию",    callback_data=ParserCb(action="clear_all"))
    kb.adjust(1)
    return kb


# ── Главное меню парсера ──────────────────────────────────────────────────

@router.callback_query(ParserCb.filter(F.action == "menu"))
async def cb_parser_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()

    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.message.edit_text(
            "🔒 <b>Парсер аудитории — PRO</b>\n\nДля доступа оформите подписку: /subscription",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1",
        callback.from_user.id,
    ) or 0
    runs = await pool.fetchval(
        "SELECT COUNT(*) FROM parser_runs WHERE owner_id=$1",
        callback.from_user.id,
    ) or 0

    await callback.message.edit_text(
        "🔍 <b>Парсер аудитории</b>\n\n"
        f"Всего в базе: <b>{total:,}</b> пользователей\n"
        f"Запусков парсера: <b>{runs}</b>\n\n"
        "Извлекайте аудиторию из каналов и групп для дальнейшей работы.\n"
        "• <b>Участники</b> — все подписчики канала/группы\n"
        "• <b>Активные</b> — кто писал в группе за последние 30 дней",
        parse_mode="HTML",
        reply_markup=_menu_kb().as_markup(),
    )


# ── Начало парсинга ──────────────────────────────────────────────────────

@router.callback_query(ParserCb.filter(F.action.in_({"start_members", "start_active"})))
async def cb_parser_start(
    callback: CallbackQuery, callback_data: ParserCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()

    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer("🔒 Требуется PRO", show_alert=True)
        return

    parse_type = "members" if callback_data.action == "start_members" else "active"
    await state.update_data(parse_type=parse_type)
    await state.set_state(ParserFSM.waiting_source)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ParserCb(action="menu"))

    type_label = "участников" if parse_type == "members" else "активных пользователей"
    extra = "" if parse_type == "members" else "\n\n⚠️ Работает только для <b>супергрупп</b> (не каналов)"

    await callback.message.edit_text(
        f"🔍 <b>Парсинг {type_label}</b>\n\n"
        "Введите <b>username</b> или ссылку на канал/группу:\n\n"
        "<code>@channelname</code>\n"
        "<code>https://t.me/channelname</code>" + extra,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ParserFSM.waiting_source)
async def fsm_parser_source(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    source = (message.text or "").strip()
    if not source:
        await message.answer("⚠️ Введите username или ссылку:")
        return

    # Нормализуем — убираем https://t.me/
    if "t.me/" in source:
        source = "@" + source.split("t.me/")[-1].split("/")[0].lstrip("+")
    if not source.startswith("@") and not source.lstrip("-").isdigit():
        source = "@" + source

    data = await state.get_data()
    parse_type = data.get("parse_type", "members")
    await state.update_data(parse_source=source)
    await state.set_state(ParserFSM.waiting_limit)

    kb = InlineKeyboardBuilder()
    kb.button(text="500",  callback_data=f"prs:limit:500")
    kb.button(text="1000", callback_data=f"prs:limit:1000")
    kb.button(text="5000", callback_data=f"prs:limit:5000")
    kb.button(text="❌ Отмена", callback_data=ParserCb(action="menu"))
    kb.adjust(3, 1)

    await message.answer(
        f"📊 <b>Источник:</b> <code>{html.escape(source)}</code>\n\n"
        "Выберите <b>максимальное количество</b> пользователей для парсинга\n"
        "или введите число вручную:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("prs:limit:"))
async def cb_parser_limit_quick(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    limit = int(callback.data.split(":")[-1])
    await _start_parse(callback.message, state, pool, callback.from_user.id, limit)


@router.message(ParserFSM.waiting_limit)
async def fsm_parser_limit(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text = (message.text or "").strip()
    try:
        limit = int(text.replace(" ", "").replace(",", ""))
        limit = max(10, min(limit, 10000))
    except ValueError:
        await message.answer("⚠️ Введите число (от 10 до 10000):")
        return
    await _start_parse(message, state, pool, message.from_user.id, limit)


async def _start_parse(
    msg_target, state: FSMContext, pool: asyncpg.Pool, owner_id: int, limit: int
) -> None:
    from services.parser import parse_members, parse_active_users

    data = await state.get_data()
    source = data.get("parse_source", "")
    parse_type = data.get("parse_type", "members")
    await state.clear()

    if not source:
        await msg_target.answer("⚠️ Источник не указан. Начните заново.")
        return

    type_label = "участников" if parse_type == "members" else "активных"
    progress_msg = await msg_target.answer(
        f"⏳ <b>Парсинг {type_label}</b>\n\n"
        f"Источник: <code>{html.escape(source)}</code>\n"
        f"Лимит: <b>{limit:,}</b>\n\n"
        "⏳ Подключаюсь...",
        parse_mode="HTML",
    )

    last_update = {"n": 0}

    async def progress_cb(current: int, total: int) -> None:
        if current - last_update["n"] >= 200:
            last_update["n"] = current
            pct = min(100, round(current / max(total, 1) * 100))
            bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)
            try:
                await progress_msg.edit_text(
                    f"⏳ <b>Парсинг {type_label}</b>\n\n"
                    f"Источник: <code>{html.escape(source)}</code>\n\n"
                    f"[{bar}] {pct}%\n"
                    f"Собрано: <b>{current:,}</b> / {total:,}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    try:
        if parse_type == "members":
            result = await parse_members(pool, owner_id, source, limit=limit, progress_cb=progress_cb)
        else:
            result = await parse_active_users(pool, owner_id, source, limit=limit, progress_cb=progress_cb)
    except Exception as e:
        await progress_msg.edit_text(
            f"❌ <b>Ошибка парсинга</b>\n\n{html.escape(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    if result.get("status") == "error":
        await progress_msg.edit_text(
            f"❌ <b>Ошибка парсинга</b>\n\n{html.escape(result.get('error', 'неизвестная ошибка')[:200])}",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Просмотреть аудиторию", callback_data=ParserCb(action="audience", run_id=result.get("run_id", 0)))
    kb.button(text="📥 Экспорт CSV",           callback_data=ParserCb(action="export",   run_id=result.get("run_id", 0)))
    kb.button(text="◀️ В меню парсера",        callback_data=ParserCb(action="menu"))
    kb.adjust(1)

    await progress_msg.edit_text(
        f"✅ <b>Парсинг завершён!</b>\n\n"
        f"Источник: <code>{html.escape(source)}</code>\n"
        f"Тип: {type_label}\n"
        f"Найдено: <b>{result.get('total_found', 0):,}</b>\n"
        f"Сохранено (новых): <b>{result.get('total_saved', 0):,}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── История запусков ──────────────────────────────────────────────────────

@router.callback_query(ParserCb.filter(F.action == "runs"))
async def cb_parser_runs(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.parser import get_run_history
    runs = await get_run_history(pool, callback.from_user.id, limit=15)

    if not runs:
        await callback.message.edit_text(
            "📋 <b>История запусков пуста</b>\n\nЗапустите парсинг через главное меню.",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = ["📋 <b>История запусков парсера</b>\n"]
    kb = InlineKeyboardBuilder()
    for r in runs:
        status_icon = {"done": "✅", "failed": "❌", "running": "⏳", "empty": "⚪"}.get(r["status"], "❓")
        started = r["started_at"].strftime("%d.%m %H:%M") if r["started_at"] else "—"
        lines.append(
            f"{status_icon} <code>{html.escape(r['source_ref'])}</code> "
            f"[{r['parse_type']}] — {r['total_found']:,} найдено\n"
            f"   <i>{started}</i>"
        )
        if r["status"] == "done" and r["total_found"] > 0:
            kb.button(
                text=f"📥 {r['source_ref'][:20]} ({r['total_found']:,})",
                callback_data=ParserCb(action="export", run_id=r["id"]),
            )

    kb.button(text="◀️ Назад", callback_data=ParserCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Просмотр аудитории ───────────────────────────────────────────────────

@router.callback_query(ParserCb.filter(F.action == "audience"))
async def cb_parser_audience(
    callback: CallbackQuery, callback_data: ParserCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    from services.parser import get_parsed_audience

    run_id = callback_data.run_id or None
    page = callback_data.page

    users = await get_parsed_audience(
        pool, callback.from_user.id,
        run_id=run_id,
        offset=page * _PAGE_SIZE,
        limit=_PAGE_SIZE,
    )

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1"
        + (" AND parse_run_id=$2" if run_id else ""),
        *([callback.from_user.id, run_id] if run_id else [callback.from_user.id]),
    ) or 0

    if not users:
        await callback.message.edit_text(
            "📊 <b>Аудитория пуста</b>",
            parse_mode="HTML",
            reply_markup=_back_kb().as_markup(),
        )
        return

    lines = [f"📊 <b>Аудитория</b> (всего: {total:,})\n"]
    for u in users:
        uname = f"@{html.escape(u['username'])}" if u.get("username") else f"ID:{u['tg_user_id']}"
        name = html.escape(u.get("first_name") or "") + (" " + html.escape(u.get("last_name") or "") if u.get("last_name") else "")
        premium = " ⭐" if u.get("is_premium") else ""
        lines.append(f"• {uname} — {name.strip()}{premium}")

    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="◀️", callback_data=ParserCb(action="audience", run_id=run_id or 0, page=page-1))
    if (page + 1) * _PAGE_SIZE < total:
        kb.button(text="▶️", callback_data=ParserCb(action="audience", run_id=run_id or 0, page=page+1))
    if run_id:
        kb.button(text="📥 Экспорт CSV", callback_data=ParserCb(action="export", run_id=run_id))
    kb.button(text="◀️ В меню", callback_data=ParserCb(action="menu"))
    kb.adjust(2, 1, 1)

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Экспорт CSV ──────────────────────────────────────────────────────────

@router.callback_query(ParserCb.filter(F.action == "export"))
async def cb_parser_export(
    callback: CallbackQuery, callback_data: ParserCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Готовлю файл...")
    from services.parser import get_parsed_audience

    run_id = callback_data.run_id or None
    users = await get_parsed_audience(
        pool, callback.from_user.id,
        run_id=run_id,
        limit=10000,
    )

    if not users:
        await callback.message.answer("⚠️ Нет данных для экспорта.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["tg_user_id", "username", "first_name", "last_name", "is_premium", "source_title", "parsed_at"])
    for u in users:
        writer.writerow([
            u["tg_user_id"],
            u.get("username") or "",
            u.get("first_name") or "",
            u.get("last_name") or "",
            "yes" if u.get("is_premium") else "no",
            u.get("source_title") or "",
            str(u.get("parsed_at", ""))[:19],
        ])

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    fname = f"audience_{run_id or 'all'}_{len(users)}.csv"
    await callback.message.answer_document(
        BufferedInputFile(csv_bytes, filename=fname),
        caption=f"📥 <b>Экспорт аудитории</b>\n{len(users):,} пользователей",
        parse_mode="HTML",
    )


# ── Очистка ──────────────────────────────────────────────────────────────

@router.callback_query(ParserCb.filter(F.action == "clear_all"))
async def cb_parser_clear(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, очистить всё", callback_data=ParserCb(action="confirm_clear"))
    kb.button(text="❌ Отмена",           callback_data=ParserCb(action="menu"))
    kb.adjust(1)
    total = await pool.fetchval(
        "SELECT COUNT(*) FROM parsed_audiences WHERE owner_id=$1",
        callback.from_user.id,
    ) or 0
    await callback.message.edit_text(
        f"⚠️ <b>Очистить всю аудиторию?</b>\n\n"
        f"Будет удалено: <b>{total:,}</b> пользователей\n"
        "<i>Это действие необратимо</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ParserCb.filter(F.action == "confirm_clear"))
async def cb_parser_confirm_clear(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.parser import delete_audience
    deleted = await delete_audience(pool, callback.from_user.id)
    await callback.message.edit_text(
        f"🗑 <b>Аудитория очищена</b>\n\nУдалено: <b>{deleted:,}</b> пользователей",
        parse_mode="HTML",
        reply_markup=_back_kb().as_markup(),
    )

"""«Клон и адаптация» в боте: мастер (образец → поля → суффикс имени → цели).

Запуск — операция `clone_adapt` в очереди (движок `services/clone_adapt_engine.py`),
полные настройки и история — в мини-аппе.
"""

import html
import logging

import aiohttp
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import CloneAdaptCb, BmCb
from bot.utils.picker_cap import cap_slice as _cap, cap_note_button as _cap_note
from bot.states import CloneAdaptFSM
from database import db
from services import bot_api
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_ALL_FIELDS = ["name", "desc", "short", "photo", "commands"]
_FIELD_LABELS = {
    "name":     "✏️ Имя",
    "desc":     "📄 Описание",
    "short":    "📋 Краткое описание",
    "photo":    "🖼 Фото",
    "commands": "🔘 Команды",
}
_PAGE_SIZE = 15


# ── helpers ───────────────────────────────────────────────────────────────────


def _default_state() -> dict:
    return {
        "source_bot_id": None,
        "fields": ["name", "desc", "short"],  # default selection
        "name_suffix": "",
        "targets": [],
        "step": "source",
    }


async def _ensure_state(state: FSMContext) -> dict:
    data = await state.get_data()
    if "step" not in data:
        data = _default_state()
        await state.set_data(data)
    return data


def _fields_kb(fields: list, source_bot_id: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for f in _ALL_FIELDS:
        mark = "✅" if f in fields else "☐"
        kb.button(
            text=f"{mark} {_FIELD_LABELS[f]}",
            callback_data=CloneAdaptCb(action="toggle_field", bot_id=source_bot_id, extra=f),
        )
    kb.button(text="▶️ Далее — выбор целей", callback_data=CloneAdaptCb(action="suffix_ask", bot_id=source_bot_id))
    kb.button(text="◀️ Назад", callback_data=CloneAdaptCb(action="start"))
    kb.adjust(1)
    return kb


def _targets_kb(all_bots, targets: list, source_bot_id: int, page: int = 0) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    start = page * _PAGE_SIZE
    chunk = all_bots[start: start + _PAGE_SIZE]
    for b in chunk:
        if b["bot_id"] == source_bot_id:
            continue
        mark = "✅" if b["bot_id"] in targets else "☐"
        label = html.escape(b["username"] or b["first_name"] or f"id{b['bot_id']}")
        kb.button(
            text=f"{mark} @{label}",
            callback_data=CloneAdaptCb(action="toggle_target", bot_id=source_bot_id, extra=str(b["bot_id"])),
        )
    n_sel = len(targets)
    nav = []
    if page > 0:
        nav.append(kb.button(text="◀️", callback_data=CloneAdaptCb(action="targets_page", bot_id=source_bot_id, page=page - 1)))
    if start + _PAGE_SIZE < len(all_bots):
        nav.append(kb.button(text="▶️", callback_data=CloneAdaptCb(action="targets_page", bot_id=source_bot_id, page=page + 1)))
    kb.button(text="✅ Выбрать все", callback_data=CloneAdaptCb(action="targets_all", bot_id=source_bot_id))
    kb.button(text="☐ Снять все", callback_data=CloneAdaptCb(action="targets_none", bot_id=source_bot_id))
    if n_sel > 0:
        kb.button(text=f"▶️ Клонировать → {n_sel}", callback_data=CloneAdaptCb(action="preview", bot_id=source_bot_id))
    kb.button(text="◀️ Назад к полям", callback_data=CloneAdaptCb(action="source", bot_id=source_bot_id))
    kb.adjust(1)
    return kb


# ── menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(CloneAdaptCb.filter(F.action == "menu"))
async def cb_ca_menu(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    await state.clear()
    try:
        rows = await pool.fetch(
            """
            SELECT h.*, sb.username AS src_uname, tb.username AS tgt_uname
            FROM clone_adapt_history h
            LEFT JOIN managed_bots sb ON sb.bot_id = h.source_bot_id
            LEFT JOIN managed_bots tb ON tb.bot_id = h.target_bot_id
            WHERE h.owner_id = $1
            ORDER BY h.created_at DESC LIMIT 10
            """,
            callback.from_user.id,
        )
    except Exception as e:
        log.error("clone_adapt_hub cb_ca_menu: %s", e)
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="growth"))
        await callback.message.edit_text(
            "🔀 <b>Клон и адаптация</b>\n\n"
            "⚠️ Модуль недоступен — таблицы не созданы в базе данных.\n\n"
            "Администратору необходимо применить миграцию <code>schema_v107.sql</code>.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Новое клонирование", callback_data=CloneAdaptCb(action="start"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="growth"))
    kb.adjust(1)

    if rows:
        icons = {"ok": "✅", "error": "❌", "partial": "⚠️", "same": "＝"}
        lines = []
        for r in rows:
            ico = icons.get(r["status"], "?")
            src = f"@{r['src_uname']}" if r["src_uname"] else f"id{r['source_bot_id']}"
            tgt = f"@{r['tgt_uname']}" if r["tgt_uname"] else f"id{r['target_bot_id']}"
            ts = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else "?"
            lines.append(f"{ico} <code>{ts}</code> {html.escape(src)} → {html.escape(tgt)}")
        history = "\n".join(lines)
    else:
        history = "История пуста."

    await callback.message.edit_text(
        "🔀 <b>Клон и адаптация</b>\n\n"
        "Копирует оформление одного бота (имя, описания, фото, команды) "
        "на несколько ботов сразу; к имени можно добавить приписку. Перед "
        "изменением сохраняется, как было, — клон можно откатить. Шаблоны "
        "имени, замены текста, переводы и предпросмотр — в приложении.\n\n"
        f"<b>Последние клонирования:</b>\n{history}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── step 1: choose source ─────────────────────────────────────────────────────


@router.callback_query(CloneAdaptCb.filter(F.action == "start"))
async def cb_ca_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await safe_answer(callback)
    await state.set_data(_default_state())
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        callback.from_user.id,
    )
    if not bots:
        await callback.message.edit_text(
            "🔀 <b>Клон и адаптация</b>\n\nУ вас нет ботов. Сначала добавьте бота.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(
                text="◀️ Назад", callback_data=CloneAdaptCb(action="menu")
            ).as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    _shown = _cap(bots)
    for b in _shown:
        label = html.escape(b["username"] or b["first_name"] or f"id{b['bot_id']}")
        kb.button(
            text=f"🤖 @{label}",
            callback_data=CloneAdaptCb(action="source", bot_id=b["bot_id"]),
        )
    _cap_note(kb, bots, _shown, what="bot")
    kb.button(text="◀️ Назад", callback_data=CloneAdaptCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🔀 <b>Шаг 1 из 3 — Выберите источник</b>\n\nЭтот бот будет образцом.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── step 2: fields selection ──────────────────────────────────────────────────


@router.callback_query(CloneAdaptCb.filter(F.action == "source"))
async def cb_ca_source(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    await safe_answer(callback)
    data = await _ensure_state(state)
    data["source_bot_id"] = callback_data.bot_id
    data["step"] = "fields"
    await state.set_data(data)

    bot_row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    label = html.escape(bot_row["username"] or bot_row["first_name"] or f"id{bot_row['bot_id']}")
    # Load current profile
    try:
        name = await bot_api.get_my_name(http, bot_row["token"])
        short = await bot_api.get_my_short_description(http, bot_row["token"])
        desc = await bot_api.get_my_description(http, bot_row["token"])
    except Exception:
        name, short, desc = "?", "?", "?"

    fields = data.get("fields", ["name", "desc", "short"])
    profile_preview = (
        f"Имя: <b>{html.escape(name or '—')}</b>\n"
        f"Краткое: {html.escape(short[:60] or '—')}\n"
        f"Описание: {html.escape(desc[:80] or '—')}"
    )
    await callback.message.edit_text(
        f"🔀 <b>Шаг 2 из 3 — Поля для клонирования</b>\n\n"
        f"Источник: @{label}\n\n{profile_preview}\n\n"
        "Отметьте поля для копирования:",
        parse_mode="HTML",
        reply_markup=_fields_kb(fields, callback_data.bot_id).as_markup(),
    )


@router.callback_query(CloneAdaptCb.filter(F.action == "toggle_field"))
async def cb_ca_toggle_field(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    data = await _ensure_state(state)
    field = callback_data.extra
    if field not in _ALL_FIELDS:
        await safe_answer(callback)
        return
    fields = list(data.get("fields", []))
    if field in fields:
        fields.remove(field)
    else:
        fields.append(field)
    data["fields"] = fields
    await state.set_data(data)
    await callback.answer(f"{'✅' if field in fields else '☐'} {_FIELD_LABELS[field]}")
    await callback.message.edit_reply_markup(
        reply_markup=_fields_kb(fields, callback_data.bot_id).as_markup()
    )


# ── optional suffix ───────────────────────────────────────────────────────────


@router.callback_query(CloneAdaptCb.filter(F.action == "suffix_ask"))
async def cb_ca_suffix_ask(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    data = await _ensure_state(state)
    if not data.get("fields"):
        await callback.answer("Выберите хотя бы одно поле.", show_alert=True)
        return
    if "name" not in data.get("fields", []):
        # No name field — skip suffix step
        await _show_targets(callback, state, data, callback_data.bot_id, pool)
        return
    await state.set_state(CloneAdaptFSM.waiting_suffix)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без суффикса", callback_data=CloneAdaptCb(action="no_suffix", bot_id=callback_data.bot_id))
    kb.button(text="◀️ Назад к полям", callback_data=CloneAdaptCb(action="source", bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        "🔀 <b>Адаптация имени (опционально)</b>\n\n"
        "Введите суффикс, который будет добавлен к концу имени каждого клонированного бота.\n\n"
        "Например: <code> [RU]</code> → «BotName [RU]»\n"
        "Или нажмите «Без суффикса» чтобы скопировать имя как есть.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(CloneAdaptFSM.waiting_suffix, F.text)
async def msg_ca_suffix(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    suffix = (message.text or "").strip()
    if len(suffix) > 32:
        await message.answer("⚠️ Суффикс не должен превышать 32 символа.", parse_mode="HTML")
        return
    await state.set_state(None)
    data["name_suffix"] = suffix
    await state.set_data(data)
    source_id = data.get("source_bot_id", 0)
    await _show_targets_msg(message, state, data, source_id, pool)


@router.callback_query(CloneAdaptCb.filter(F.action == "no_suffix"))
async def cb_ca_no_suffix(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    await state.set_state(None)
    data = await _ensure_state(state)
    data["name_suffix"] = ""
    await state.set_data(data)
    await _show_targets(callback, state, data, callback_data.bot_id, pool)


# ── step 3: choose targets ────────────────────────────────────────────────────


async def _show_targets(callback: CallbackQuery, state: FSMContext, data: dict, source_id: int, pool, page: int = 0) -> None:
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        callback.from_user.id,
    )
    targets = data.get("targets", [])
    suffix = data.get("name_suffix", "")
    suffix_info = f"\nСуффикс имени: <code>{html.escape(suffix)}</code>" if suffix else ""
    await callback.message.edit_text(
        f"🔀 <b>Шаг 3 из 3 — Выберите цели</b>{suffix_info}\n\n"
        "Отметьте боты, на которые нужно скопировать профиль:",
        parse_mode="HTML",
        reply_markup=_targets_kb(bots, targets, source_id, page).as_markup(),
    )


async def _show_targets_msg(message: Message, state: FSMContext, data: dict, source_id: int, pool: asyncpg.Pool, page: int = 0) -> None:
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        message.from_user.id,
    )
    targets = data.get("targets", [])
    suffix = data.get("name_suffix", "")
    suffix_info = f"\nСуффикс имени: <code>{html.escape(suffix)}</code>" if suffix else ""
    await message.answer(
        f"🔀 <b>Шаг 3 из 3 — Выберите цели</b>{suffix_info}\n\n"
        "Отметьте боты, на которые нужно скопировать профиль:",
        parse_mode="HTML",
        reply_markup=_targets_kb(bots, targets, source_id, page).as_markup(),
    )


@router.callback_query(CloneAdaptCb.filter(F.action == "toggle_target"))
async def cb_ca_toggle_target(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await _ensure_state(state)
    try:
        tid = int(callback_data.extra)
    except (ValueError, TypeError):
        await safe_answer(callback)
        return
    targets = list(data.get("targets", []))
    if tid in targets:
        targets.remove(tid)
    else:
        targets.append(tid)
    data["targets"] = targets
    await state.set_data(data)
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        callback.from_user.id,
    )
    await callback.answer(f"{'✅' if tid in targets else '☐'}")
    await callback.message.edit_reply_markup(
        reply_markup=_targets_kb(bots, targets, callback_data.bot_id).as_markup()
    )


@router.callback_query(CloneAdaptCb.filter(F.action == "targets_all"))
async def cb_ca_targets_all(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await _ensure_state(state)
    bots = await pool.fetch(
        "SELECT bot_id FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id", callback.from_user.id
    )
    targets = [b["bot_id"] for b in bots if b["bot_id"] != callback_data.bot_id]
    data["targets"] = targets
    await state.set_data(data)
    await callback.answer(f"✅ Выбрано {len(targets)}")
    all_bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        callback.from_user.id,
    )
    await callback.message.edit_reply_markup(
        reply_markup=_targets_kb(all_bots, targets, callback_data.bot_id).as_markup()
    )


@router.callback_query(CloneAdaptCb.filter(F.action == "targets_none"))
async def cb_ca_targets_none(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await _ensure_state(state)
    data["targets"] = []
    await state.set_data(data)
    await callback.answer("☐ Снято")
    all_bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        callback.from_user.id,
    )
    await callback.message.edit_reply_markup(
        reply_markup=_targets_kb(all_bots, [], callback_data.bot_id).as_markup()
    )


@router.callback_query(CloneAdaptCb.filter(F.action == "targets_page"))
async def cb_ca_targets_page(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    data = await _ensure_state(state)
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        callback.from_user.id,
    )
    targets = data.get("targets", [])
    suffix = data.get("name_suffix", "")
    suffix_info = f"\nСуффикс: <code>{html.escape(suffix)}</code>" if suffix else ""
    await callback.message.edit_text(
        f"🔀 <b>Шаг 3 из 3 — Выберите цели</b>{suffix_info}",
        parse_mode="HTML",
        reply_markup=_targets_kb(bots, targets, callback_data.bot_id, callback_data.page).as_markup(),
    )


# ── preview ───────────────────────────────────────────────────────────────────


@router.callback_query(CloneAdaptCb.filter(F.action == "preview"))
async def cb_ca_preview(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    data = await _ensure_state(state)
    targets = data.get("targets", [])
    fields = data.get("fields", [])
    suffix = data.get("name_suffix", "")
    if not targets:
        await callback.answer("Выберите хотя бы одну цель.", show_alert=True)
        return
    if not fields:
        await callback.answer("Выберите хотя бы одно поле.", show_alert=True)
        return

    target_bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE bot_id = ANY($1::bigint[]) AND added_by=$2 AND is_active=TRUE",
        targets, callback.from_user.id,
    )
    source_bot = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    src_label = html.escape(source_bot["username"] or source_bot["first_name"] or f"id{source_bot['bot_id']}") if source_bot else "?"

    fields_str = " • ".join(_FIELD_LABELS[f] for f in fields if f in _FIELD_LABELS)
    tgt_list = "\n".join(
        f"  🤖 @{html.escape(b['username'] or b['first_name'] or str(b['bot_id']))}"
        for b in target_bots
    )
    suffix_info = f"\n  Суффикс к имени: <code>{html.escape(suffix)}</code>" if suffix else ""

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить клонирование", callback_data=CloneAdaptCb(action="run", bot_id=callback_data.bot_id))
    kb.button(text="◀️ Назад", callback_data=CloneAdaptCb(action="source", bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🔀 <b>Подтверждение клонирования</b>\n\n"
        f"Источник: @{src_label}\n"
        f"Поля: {fields_str}{suffix_info}\n"
        f"Целей: <b>{len(target_bots)}</b>\n\n"
        f"{tgt_list}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── run ───────────────────────────────────────────────────────────────────────


@router.callback_query(CloneAdaptCb.filter(F.action == "run"))
async def cb_ca_run(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Запуск — операцией в очереди, тем же движком, что в мини-аппе.

    Раньше бот клонировал прямо в обработчике кнопки: мимо очереди, без
    журнала операции, без снимка для отката, а сбой посреди списка целей
    терял и то, что успело примениться."""
    from services import operation_bus as _obus

    await safe_answer(callback)
    data = await _ensure_state(state)
    targets = [int(t) for t in data.get("targets", []) if t]
    fields = [f for f in data.get("fields", []) if f in _ALL_FIELDS]
    suffix = data.get("name_suffix", "")
    source_bot_id = callback_data.bot_id

    if not targets or not fields:
        await callback.answer("Нечего клонировать.", show_alert=True)
        return

    source = await db.get_bot(pool, source_bot_id, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    if not source:
        kb.button(text="◀️ Назад", callback_data=CloneAdaptCb(action="menu"))
        await callback.message.edit_text("❌ Бот-образец не найден.", reply_markup=kb.as_markup())
        return

    settings = {"name_template": "{name}" + suffix if suffix else "{name}"}
    src_label = source["username"] or source["first_name"] or f"id{source_bot_id}"
    try:
        op_id = await _obus.submit(
            pool, callback.from_user.id, "clone_adapt", {
                "source_bot_id": source_bot_id,
                "target_bot_ids": targets,
                "fields": fields,
                "settings": settings,
            }, total_items=len(targets),
            label=f"Клон @{src_label} → ботов: {len(targets)}")
    except PermissionError as exc:
        kb.button(text="◀️ К меню", callback_data=CloneAdaptCb(action="menu"))
        await callback.message.edit_text(
            f"🔒 {html.escape(str(exc) or 'Клон доступен на платном тарифе')}",
            parse_mode="HTML", reply_markup=kb.as_markup())
        return
    except Exception:
        log.exception("clone_adapt_hub: не поставлен клон uid=%s", callback.from_user.id)
        kb.button(text="◀️ К меню", callback_data=CloneAdaptCb(action="menu"))
        await callback.message.edit_text(
            "❌ Не удалось поставить клон в очередь. Попробуйте ещё раз.",
            reply_markup=kb.as_markup())
        return

    await state.clear()
    kb.button(text="🔀 Ещё клонирование", callback_data=CloneAdaptCb(action="start"))
    kb.button(text="◀️ К меню", callback_data=CloneAdaptCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Клон поставлен в очередь</b> (операция #{op_id})\n\n"
        f"Образец: @{html.escape(src_label)}\n"
        f"Целей: <b>{len(targets)}</b>\n\n"
        "У каждого бота перед изменением сохраняется, как было, — клон можно "
        "откатить. Подробности по каждому полю, предпросмотр, замены текста и "
        "переводы — в приложении: «Каналы и боты» → «Клон и адаптация».",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

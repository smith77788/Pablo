"""Clone & Adapt UI — clone bot profiles (name/desc/short/photo/commands) to multiple targets."""

import asyncio
import html
import logging

import aiohttp
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import CloneAdaptCb, BmCb
from bot.states import CloneAdaptFSM
from database import db
from services import bot_api

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
    await callback.answer()
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
        kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
        await callback.message.edit_text(
            "🔀 <b>Clone & Adapt</b>\n\n"
            "⚠️ Модуль недоступен — таблицы не созданы в базе данных.\n\n"
            "Администратору необходимо применить миграцию <code>schema_v107.sql</code>.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Новое клонирование", callback_data=CloneAdaptCb(action="start"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="operations"))
    kb.adjust(1)

    if rows:
        icons = {"ok": "✅", "error": "❌", "partial": "⚠️"}
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
        "🔀 <b>Clone & Adapt</b>\n\n"
        "Копирует профиль одного бота (имя, описание, фото, команды) "
        "на несколько ботов сразу. Опционально: добавляет суффикс к имени "
        "для различия ботов в Telegram Search.\n\n"
        f"<b>Последние клонирования:</b>\n{history}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── step 1: choose source ─────────────────────────────────────────────────────


@router.callback_query(CloneAdaptCb.filter(F.action == "start"))
async def cb_ca_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    await state.set_data(_default_state())
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        callback.from_user.id,
    )
    if not bots:
        await callback.message.edit_text(
            "🔀 <b>Clone & Adapt</b>\n\nУ вас нет ботов. Сначала добавьте бота.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(
                text="◀️ Назад", callback_data=CloneAdaptCb(action="menu")
            ).as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for b in bots:
        label = html.escape(b["username"] or b["first_name"] or f"id{b['bot_id']}")
        kb.button(
            text=f"🤖 @{label}",
            callback_data=CloneAdaptCb(action="source", bot_id=b["bot_id"]),
        )
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
    await callback.answer()
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
        await callback.answer()
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
    await callback.answer()
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
    await callback.answer()
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
        await callback.answer()
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
    await callback.answer()
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
    await callback.answer()
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


async def _clone_to_bot(
    http: aiohttp.ClientSession,
    pool: asyncpg.Pool,
    owner_id: int,
    source: dict,
    target: dict,
    fields: list,
    suffix: str,
    src_name: str,
    src_desc: str,
    src_short: str,
    src_photo_bytes: bytes | None,
    src_commands: list,
) -> tuple[bool, str]:
    """Clone selected fields from source to one target bot. Returns (ok, detail)."""
    errors = []
    ok_count = 0

    if "name" in fields:
        new_name = (src_name + suffix)[:64]
        ok = await bot_api.set_name(http, target["token"], new_name)
        if ok:
            ok_count += 1
        else:
            errors.append("имя")

    if "desc" in fields:
        ok = await bot_api.set_description(http, target["token"], src_desc[:512])
        if ok:
            ok_count += 1
        else:
            errors.append("описание")

    if "short" in fields:
        ok = await bot_api.set_short_description(http, target["token"], src_short[:120])
        if ok:
            ok_count += 1
        else:
            errors.append("краткое описание")

    if "photo" in fields and src_photo_bytes:
        ok = await bot_api.set_photo(http, target["token"], src_photo_bytes)
        if ok:
            ok_count += 1
        else:
            errors.append("фото")

    if "commands" in fields and src_commands is not None:
        ok = await bot_api.set_my_commands(http, target["token"], src_commands)
        if ok:
            ok_count += 1
        else:
            errors.append("команды")

    status = "ok" if not errors else "error"
    detail = f"Ошибки: {', '.join(errors)}" if errors else f"OK ({ok_count} полей)"
    return status == "ok", detail


@router.callback_query(CloneAdaptCb.filter(F.action == "run"))
async def cb_ca_run(
    callback: CallbackQuery, callback_data: CloneAdaptCb, state: FSMContext,
    pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    await callback.answer()
    data = await _ensure_state(state)
    targets = data.get("targets", [])
    fields = data.get("fields", [])
    suffix = data.get("name_suffix", "")
    source_bot_id = callback_data.bot_id

    if not targets or not fields:
        await callback.answer("Нечего клонировать.", show_alert=True)
        return

    await callback.message.edit_text("⏳ Клонирую профиль…", parse_mode="HTML")

    source = await db.get_bot(pool, source_bot_id, callback.from_user.id)
    if not source:
        await callback.message.edit_text("❌ Источник не найден.")
        return

    # Fetch source profile
    src_name, src_desc, src_short, src_commands = "", "", "", []
    try:
        src_name, src_desc, src_short = await asyncio.gather(
            bot_api.get_my_name(http, source["token"]),
            bot_api.get_my_description(http, source["token"]),
            bot_api.get_my_short_description(http, source["token"]),
        )
    except Exception as e:
        log.debug("Clone & Adapt: failed to fetch source profile: %s", e)

    if "commands" in fields:
        try:
            src_commands = await bot_api.get_my_commands(http, source["token"])
        except Exception:
            src_commands = []

    # Fetch source photo
    src_photo_bytes: bytes | None = None
    if "photo" in fields:
        try:
            me = await bot_api.get_me(http, source["token"])
            if me:
                bot_uid = me.get("id")
                # getUserProfilePhotos to get file_id
                phdata = await bot_api._call(http, source["token"], "getUserProfilePhotos", user_id=bot_uid, limit=1)
                if phdata.get("ok"):
                    photos = phdata.get("result", {}).get("photos", [])
                    if photos:
                        # Largest version is last in the array
                        file_id = photos[0][-1]["file_id"]
                        fdata = await bot_api._call(http, source["token"], "getFile", file_id=file_id)
                        if fdata.get("ok"):
                            file_path = fdata["result"]["file_path"]
                            token = source["token"]
                            dl_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                            async with http.get(dl_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                                if resp.status == 200:
                                    src_photo_bytes = await resp.read()
        except Exception as e:
            log.debug("Clone & Adapt: failed to fetch source photo: %s", e)

    # Get target bots
    target_bots = await pool.fetch(
        "SELECT * FROM managed_bots WHERE bot_id = ANY($1::bigint[]) AND added_by=$2 AND is_active=TRUE",
        targets, callback.from_user.id,
    )

    results = []
    for tb in target_bots:
        try:
            ok, detail = await _clone_to_bot(
                http, pool, callback.from_user.id,
                source, dict(tb), fields, suffix,
                src_name, src_desc, src_short, src_photo_bytes, src_commands,
            )
            label = tb["username"] or tb["first_name"] or f"id{tb['bot_id']}"
            results.append((label, ok, detail))
            await pool.execute(
                """
                INSERT INTO clone_adapt_history (owner_id, source_bot_id, target_bot_id, fields, status, details)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                callback.from_user.id, source_bot_id, tb["bot_id"],
                ",".join(fields), "ok" if ok else "error", detail,
            )
        except Exception as e:
            label = tb["username"] or tb["first_name"] or f"id{tb['bot_id']}"
            results.append((label, False, str(e)[:80]))

    await state.clear()

    # Build result message
    ok_cnt = sum(1 for _, ok, _ in results if ok)
    lines = []
    for label, ok, detail in results:
        ico = "✅" if ok else "❌"
        lines.append(f"{ico} @{html.escape(label)} — {html.escape(detail)}")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔀 Ещё клонирование", callback_data=CloneAdaptCb(action="start"))
    kb.button(text="◀️ К меню", callback_data=CloneAdaptCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🔀 <b>Клонирование завершено</b>\n\n"
        f"✅ Успешно: {ok_cnt}/{len(results)}\n\n"
        + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

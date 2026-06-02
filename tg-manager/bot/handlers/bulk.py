"""Bulk operations: apply profile changes to ALL managed bots simultaneously."""

from __future__ import annotations
import asyncio
import aiohttp
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import BulkCb
from bot.keyboards import bulk_menu, main_menu
from bot.states import BulkEdit, ImportBots
from database import db
from services import bot_api

router = Router()

_LANG_HINT = (
    "Введите код языка (<code>ru</code>, <code>en</code>, <code>uk</code>, <code>de</code>…) "
    "или <code>-</code> чтобы сбросить до дефолтного."
)


async def _apply_all(
    pool: asyncpg.Pool, user_id: int, http: aiohttp.ClientSession, method, *args
) -> tuple[int, int, int]:
    """Call method(http, token, *args) on all user bots concurrently.
    Returns (success_count, failed_count, total)."""
    bots = await db.get_bots(pool, user_id)
    if not bots:
        return 0, 0, 0
    results = await asyncio.gather(
        *(method(http, b["token"], *args) for b in bots),
        return_exceptions=True,
    )
    success = sum(1 for r in results if r is True)
    return success, len(results) - success, len(results)


def _result_text(ok: int, fail: int, total: int, action: str) -> str:
    return (
        f"📦 <b>Результат массового применения</b>\n\n"
        f"Действие: {action}\n"
        f"Всего ботов: {total}\n"
        f"✅ Успешно: {ok}\n"
        f"❌ Ошибок: {fail}"
    )


# ── Menu ──────────────────────────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "menu"))
async def cb_bulk_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:

    await callback.answer()
    total = len(await db.get_bots(pool, callback.from_user.id))
    await callback.message.edit_text(
        f"📦 <b>Массовые операции</b>\n\nБотов в системе: <b>{total}</b>\n\n"
        "Выбранное действие применяется ко всем ботам сразу:",
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )
    await callback.answer()


# ── Token check ───────────────────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "check"))
async def cb_check(
    callback: CallbackQuery, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:

    bots = await db.get_bots(pool, callback.from_user.id)
    if not bots:
        await callback.answer("Нет ботов для проверки.", show_alert=True)
        return
    await callback.answer()

    await callback.message.edit_text(f"⏳ Проверяю {len(bots)} токенов...")

    tokens = [b["token"] for b in bots]
    results = await bot_api.batch_get_me(http, tokens)

    ok_labels, fail_labels = [], []
    for b in bots:
        label = f"@{b['username']}" if b["username"] else b["first_name"]
        if results.get(b["token"]):
            ok_labels.append(f"✅ {label}")
        else:
            fail_labels.append(f"❌ {label}")

    lines = ok_labels + fail_labels
    text = (
        f"🔍 <b>Проверка токенов</b>\n"
        f"Активных: {len(ok_labels)} | Недоступных: {len(fail_labels)}\n\n"
        + "\n".join(lines[:50])
    )
    if len(lines) > 50:
        text += f"\n…и ещё {len(lines) - 50}"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=bulk_menu())
    await callback.answer()


# ── Bulk name (default) ───────────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "name"))
async def cb_bulk_name(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "✏️ <b>Имя для всех ботов</b>\n\nВведите новое имя:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_name, F.text)
async def msg_bulk_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    name = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_name, name
    )
    await msg.edit_text(
        _result_text(ok, fail, total, f"Имя → «{name[:30]}»"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Bulk name by GEO ──────────────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "name_lang"))
async def cb_bulk_name_lang(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_name_lang)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🌍 <b>Имя по языку — для всех ботов</b>\n\n{_LANG_HINT}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_name_lang, F.text)
async def msg_bulk_name_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✏️ Введите имя для языка <code>{message.text.strip()}</code>:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_localized_name, F.text)
async def msg_bulk_localized_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    name = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_name, name, lang
    )
    await msg.edit_text(
        _result_text(ok, fail, total, f"Имя [{lang or 'default'}] → «{name[:30]}»"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Bulk description (default) ────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "desc"))
async def cb_bulk_desc(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_desc)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📄 <b>Описание для всех ботов</b>\n\nВведите новое описание:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_desc, F.text)
async def msg_bulk_desc(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    desc = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_description, desc
    )
    await msg.edit_text(
        _result_text(ok, fail, total, "Описание обновлено"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Bulk description by GEO ───────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "desc_lang"))
async def cb_bulk_desc_lang(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_desc_lang)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🌍 <b>Описание по языку — для всех ботов</b>\n\n{_LANG_HINT}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_desc_lang, F.text)
async def msg_bulk_desc_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_desc)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await message.answer("📄 Введите описание:", reply_markup=kb.as_markup())


@router.message(BulkEdit.waiting_localized_desc, F.text)
async def msg_bulk_localized_desc(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    desc = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_description, desc, lang
    )
    await msg.edit_text(
        _result_text(ok, fail, total, f"Описание [{lang or 'default'}]"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Bulk short description (default) ─────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "short"))
async def cb_bulk_short(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_short)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📃 <b>Краткое описание для всех ботов</b>\n\nВведите текст (до 120 символов):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_short, F.text)
async def msg_bulk_short(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    short = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_short_description, short
    )
    await msg.edit_text(
        _result_text(ok, fail, total, "Краткое описание обновлено"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Bulk short description by GEO ────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "short_lang"))
async def cb_bulk_short_lang(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_short_lang)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🌍 <b>Краткое описание по языку — для всех ботов</b>\n\n{_LANG_HINT}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_short_lang, F.text)
async def msg_bulk_short_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_short)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await message.answer("📃 Введите краткое описание:", reply_markup=kb.as_markup())


@router.message(BulkEdit.waiting_localized_short, F.text)
async def msg_bulk_localized_short(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    short = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_short_description, short, lang
    )
    await msg.edit_text(
        _result_text(ok, fail, total, f"Краткое [{lang or 'default'}]"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Bulk commands (default) ───────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "commands"))
async def cb_bulk_commands(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_commands)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🤖 <b>Команды для всех ботов (по умолчанию)</b>\n\n"
        "Отправьте список команд, каждая с новой строки:\n\n"
        "<code>start - Главное меню\n"
        "help - Помощь\n"
        "about - О боте</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_commands, F.text)
async def msg_bulk_commands(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    from bot.handlers.commands import _parse_commands

    commands = _parse_commands(message.text or "")
    if not commands:
        await message.answer(
            "❌ Неверный формат. Каждая строка:\n<code>/команда - Описание</code>",
            parse_mode="HTML",
        )
        return
    await state.clear()
    msg = await message.answer("⏳ Применяю команды ко всем ботам…")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_my_commands, commands, ""
    )
    await msg.edit_text(
        _result_text(ok, fail, total, "Команды установлены"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Bulk commands by GEO ──────────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "commands_lang"))
async def cb_bulk_commands_lang(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BulkEdit.waiting_commands_lang)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🌍 <b>Команды по языку — для всех ботов</b>\n\n{_LANG_HINT}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_commands_lang, F.text)
async def msg_bulk_commands_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_commands)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "🤖 Отправьте список команд:\n\n"
        "<code>start - Главное меню\n"
        "help - Помощь</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkEdit.waiting_localized_commands, F.text)
async def msg_bulk_localized_commands(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    from bot.handlers.commands import _parse_commands

    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    commands = _parse_commands(message.text or "")
    if not commands:
        await message.answer(
            "❌ Неверный формат. Каждая строка:\n<code>/команда - Описание</code>",
            parse_mode="HTML",
        )
        return
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам…")
    ok, fail, total = await _apply_all(
        pool, message.from_user.id, http, bot_api.set_my_commands, commands, lang
    )
    await msg.edit_text(
        _result_text(ok, fail, total, f"Команды [{lang or 'default'}]"),
        parse_mode="HTML",
        reply_markup=bulk_menu(),
    )


# ── Import multiple bots ──────────────────────────────────────────────────


@router.callback_query(BulkCb.filter(F.action == "import"))
async def cb_import(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ImportBots.waiting_tokens)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BulkCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📥 <b>Массовый импорт ботов</b>\n\n"
        "Отправьте токены ботов — по одному на строке:\n\n"
        "<code>123456789:AAF...\n"
        "987654321:BBG...\n"
        "555555555:CCH...</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ImportBots.waiting_tokens, F.text)
async def msg_import_tokens(
    message: Message, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    await state.clear()
    lines = [l.strip() for l in (message.text or "").strip().splitlines() if l.strip()]
    if not lines:
        await message.answer(
            "❌ Не найдено ни одного токена.", reply_markup=main_menu()
        )
        return

    progress = await message.answer(f"⏳ Проверяю {len(lines)} токенов…")

    results = await asyncio.gather(
        *(bot_api.get_me(http, t) for t in lines),
        return_exceptions=True,
    )

    added, skipped, failed = [], [], []
    for token, info in zip(lines, results):
        if isinstance(info, Exception) or not info:
            failed.append(f"❌ {token[:25]}…")
            continue
        ok = await db.add_bot(
            pool,
            token=token,
            bot_id=info["id"],
            username=info.get("username", ""),
            first_name=info.get("first_name", ""),
            added_by=message.from_user.id,
        )
        label = f"@{info.get('username') or info.get('first_name', str(info['id']))}"
        if ok:
            added.append(f"✅ {label}")
        else:
            skipped.append(f"⚠️ {label} (уже есть)")

    parts = []
    if added:
        parts.append(f"✅ Добавлено: <b>{len(added)}</b>")
    if skipped:
        parts.append(f"⚠️ Уже были: <b>{len(skipped)}</b>")
    if failed:
        parts.append(f"❌ Ошибок: <b>{len(failed)}</b>")

    all_labels = added + skipped + failed
    detail = "\n".join(all_labels[:30])
    if len(all_labels) > 30:
        detail += f"\n…и ещё {len(all_labels) - 30}"

    await progress.edit_text(
        "📥 <b>Результат импорта</b>\n\n"
        + "\n".join(parts)
        + (f"\n\n{detail}" if detail else ""),
        parse_mode="HTML",
        reply_markup=main_menu(),
    )

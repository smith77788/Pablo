"""Bulk operations merged into the network module — apply changes to all bots at once."""
from __future__ import annotations
import asyncio
import aiohttp
import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from bot.callbacks import NetworkCb, BulkCb, BotCb
from bot.keyboards import network_ops_menu, main_menu, subscription_locked_markup
from bot.states import BulkEdit, ImportBots
from bot.utils.subscription import require_plan, locked_text, is_platform_admin
from database import db
from services import bot_api

router = Router()

_LANG_HINT = (
    "Введите код языка (<code>ru</code>, <code>en</code>, <code>uk</code>, <code>de</code>…) "
    "или <code>-</code> чтобы сбросить до дефолтного."
)


async def _apply_all(pool: asyncpg.Pool, user_id: int,
                     http: aiohttp.ClientSession, method, *args) -> tuple[int, int, int]:
    bots = await db.get_bots(pool, user_id)
    if not bots:
        return 0, 0, 0
    results = await asyncio.gather(
        *(method(http, b["token"], *args) for b in bots),
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    return ok, len(results) - ok, len(results)


def _result_text(ok: int, fail: int, total: int, action: str) -> str:
    return (
        f"📦 <b>Результат массового применения</b>\n\n"
        f"Действие: {action}\n"
        f"Всего ботов: {total}\n✅ Успешно: {ok}\n❌ Ошибок: {fail}"
    )


async def _check_pro(callback: CallbackQuery, pool: asyncpg.Pool) -> bool:
    if await require_plan(pool, callback.from_user.id, "pro"):
        return True
    await callback.message.edit_text(
        locked_text("Массовые операции", "pro"), parse_mode="HTML",
        reply_markup=subscription_locked_markup("pro"),
    )
    return False


# ── NetworkCb bulk actions ─────────────────────────────────────────────────────

@router.callback_query(NetworkCb.filter(F.action == "bulk_check"))
async def cb_bulk_check(callback: CallbackQuery, pool: asyncpg.Pool,
                        http: aiohttp.ClientSession) -> None:
    if not await _check_pro(callback, pool):
        return
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
        (ok_labels if results.get(b["token"]) else fail_labels).append(
            f"{'✅' if results.get(b['token']) else '❌'} {label}"
        )
    lines = ok_labels + fail_labels
    text = (
        f"🔍 <b>Проверка токенов</b>\n"
        f"Активных: {len(ok_labels)} | Недоступных: {len(fail_labels)}\n\n"
        + "\n".join(lines[:50])
    )
    if len(lines) > 50:
        text += f"\n…и ещё {len(lines) - 50}"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_name"))
async def cb_bulk_name(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_name)
    await callback.message.edit_text("✏️ <b>Имя для всех ботов</b>\n\nВведите новое имя:", parse_mode="HTML")


@router.message(BulkEdit.waiting_name, F.text)
async def msg_bulk_name(message: Message, state: FSMContext,
                        pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    name = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_name, name)
    await msg.edit_text(_result_text(ok, fail, total, f"Имя → «{name[:30]}»"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_name_lang"))
async def cb_bulk_name_lang(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_name_lang)
    await callback.message.edit_text(f"🌍 <b>Имя по языку — для всех ботов</b>\n\n{_LANG_HINT}", parse_mode="HTML")


@router.message(BulkEdit.waiting_name_lang, F.text)
async def msg_bulk_name_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_name)
    await message.answer(f"✏️ Введите имя для языка <code>{message.text.strip()}</code>:", parse_mode="HTML")


@router.message(BulkEdit.waiting_localized_name, F.text)
async def msg_bulk_localized_name(message: Message, state: FSMContext,
                                   pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    name = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_name, name, lang)
    await msg.edit_text(_result_text(ok, fail, total, f"Имя [{lang or 'default'}] → «{name[:30]}»"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_desc"))
async def cb_bulk_desc(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_desc)
    await callback.message.edit_text("📄 <b>Описание для всех ботов</b>\n\nВведите новое описание:", parse_mode="HTML")


@router.message(BulkEdit.waiting_desc, F.text)
async def msg_bulk_desc(message: Message, state: FSMContext,
                        pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    desc = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_description, desc)
    await msg.edit_text(_result_text(ok, fail, total, "Описание обновлено"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_desc_lang"))
async def cb_bulk_desc_lang(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_desc_lang)
    await callback.message.edit_text(f"🌍 <b>Описание по языку — для всех ботов</b>\n\n{_LANG_HINT}", parse_mode="HTML")


@router.message(BulkEdit.waiting_desc_lang, F.text)
async def msg_bulk_desc_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_desc)
    await message.answer("📄 Введите описание:")


@router.message(BulkEdit.waiting_localized_desc, F.text)
async def msg_bulk_localized_desc(message: Message, state: FSMContext,
                                   pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    desc = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_description, desc, lang)
    await msg.edit_text(_result_text(ok, fail, total, f"Описание [{lang or 'default'}]"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_short"))
async def cb_bulk_short(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_short)
    await callback.message.edit_text("📃 <b>Краткое описание для всех ботов</b>\n\nВведите текст:", parse_mode="HTML")


@router.message(BulkEdit.waiting_short, F.text)
async def msg_bulk_short(message: Message, state: FSMContext,
                          pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    short = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_short_description, short)
    await msg.edit_text(_result_text(ok, fail, total, "Краткое описание обновлено"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_short_lang"))
async def cb_bulk_short_lang(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_short_lang)
    await callback.message.edit_text(f"🌍 <b>Краткое по языку — для всех ботов</b>\n\n{_LANG_HINT}", parse_mode="HTML")


@router.message(BulkEdit.waiting_short_lang, F.text)
async def msg_bulk_short_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_short)
    await message.answer("📃 Введите краткое описание:")


@router.message(BulkEdit.waiting_localized_short, F.text)
async def msg_bulk_localized_short(message: Message, state: FSMContext,
                                    pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    short = message.text.strip()
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам...")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_short_description, short, lang)
    await msg.edit_text(_result_text(ok, fail, total, f"Краткое [{lang or 'default'}]"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_commands"))
async def cb_bulk_commands(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_commands)
    await callback.message.edit_text(
        "🤖 <b>Команды для всех ботов</b>\n\n"
        "Отправьте список команд, каждая с новой строки:\n\n"
        "<code>start - Главное меню\nhelp - Помощь</code>",
        parse_mode="HTML",
    )


@router.message(BulkEdit.waiting_commands, F.text)
async def msg_bulk_commands(message: Message, state: FSMContext,
                             pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    from bot.handlers.commands import _parse_commands
    commands = _parse_commands(message.text or "")
    if not commands:
        await message.answer("❌ Неверный формат. Каждая строка: <code>/команда - Описание</code>", parse_mode="HTML")
        return
    await state.clear()
    msg = await message.answer("⏳ Применяю команды ко всем ботам…")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_my_commands, commands, "")
    await msg.edit_text(_result_text(ok, fail, total, "Команды установлены"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_commands_lang"))
async def cb_bulk_commands_lang(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    if not await _check_pro(callback, pool):
        return
    await state.set_state(BulkEdit.waiting_commands_lang)
    await callback.message.edit_text(f"🌍 <b>Команды по языку — для всех ботов</b>\n\n{_LANG_HINT}", parse_mode="HTML")


@router.message(BulkEdit.waiting_commands_lang, F.text)
async def msg_bulk_commands_lang(message: Message, state: FSMContext) -> None:
    await state.update_data(lang=message.text.strip())
    await state.set_state(BulkEdit.waiting_localized_commands)
    await message.answer("🤖 Отправьте список команд:\n\n<code>start - Главное меню\nhelp - Помощь</code>", parse_mode="HTML")


@router.message(BulkEdit.waiting_localized_commands, F.text)
async def msg_bulk_localized_commands(message: Message, state: FSMContext,
                                       pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    from bot.handlers.commands import _parse_commands
    data = await state.get_data()
    lang = "" if data["lang"] == "-" else data["lang"]
    commands = _parse_commands(message.text or "")
    if not commands:
        await message.answer("❌ Неверный формат. Каждая строка: <code>/команда - Описание</code>", parse_mode="HTML")
        return
    await state.clear()
    msg = await message.answer("⏳ Применяю ко всем ботам…")
    ok, fail, total = await _apply_all(pool, message.from_user.id, http, bot_api.set_my_commands, commands, lang)
    await msg.edit_text(_result_text(ok, fail, total, f"Команды [{lang or 'default'}]"),
                        parse_mode="HTML", reply_markup=network_ops_menu())


@router.callback_query(NetworkCb.filter(F.action == "bulk_import"))
async def cb_bulk_import(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    # Import доступен без подписки
    await state.set_state(ImportBots.waiting_tokens)
    await callback.message.edit_text(
        "📥 <b>Массовый импорт ботов</b>\n\n"
        "Отправьте токены ботов — по одному на строке:\n\n"
        "<code>123456789:AAF...\n987654321:BBG...</code>",
        parse_mode="HTML",
    )


@router.message(ImportBots.waiting_tokens, F.text)
async def msg_import_tokens(message: Message, state: FSMContext,
                             pool: asyncpg.Pool, http: aiohttp.ClientSession) -> None:
    await state.clear()
    lines = [l.strip() for l in (message.text or "").strip().splitlines() if l.strip()]
    if not lines:
        await message.answer("❌ Не найдено ни одного токена.", reply_markup=main_menu(is_admin=is_platform_admin(message.from_user.id)))
        return
    progress = await message.answer(f"⏳ Проверяю {len(lines)} токенов…")
    import asyncio as _aio
    results = await _aio.gather(*(bot_api.get_me(http, t) for t in lines), return_exceptions=True)
    added, skipped, failed = [], [], []
    for token, info in zip(lines, results):
        if isinstance(info, Exception) or not info:
            failed.append(f"❌ {token[:25]}…")
            continue
        ok = await db.add_bot(pool, token=token, bot_id=info["id"],
                               username=info.get("username", ""),
                               first_name=info.get("first_name", ""),
                               added_by=message.from_user.id)
        label = f"@{info.get('username') or info.get('first_name', str(info['id']))}"
        (added if ok else skipped).append(f"{'✅' if ok else '⚠️'} {label}")
    parts = []
    if added: parts.append(f"✅ Добавлено: <b>{len(added)}</b>")
    if skipped: parts.append(f"⚠️ Уже были: <b>{len(skipped)}</b>")
    if failed: parts.append(f"❌ Ошибок: <b>{len(failed)}</b>")
    detail = "\n".join((added + skipped + failed)[:30])
    await progress.edit_text(
        f"📥 <b>Результат импорта</b>\n\n" + "\n".join(parts) + (f"\n\n{detail}" if detail else ""),
        parse_mode="HTML", reply_markup=main_menu(is_admin=is_platform_admin(message.from_user.id)),
    )


# ── Backward compat: old BulkCb handlers redirect to network ops ──────────────

@router.callback_query(BulkCb.filter(F.action == "menu"))
async def cb_bulk_menu_compat(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    ov = await db.get_network_overview(pool, callback.from_user.id)
    await callback.message.edit_text(
        f"🌐 <b>Сеть & массовые операции</b>\n\n"
        f"🤖 Ботов: <b>{ov['total_bots']}</b> | 👤 Юзеров: <b>{ov['unique_users']:,}</b>",
        parse_mode="HTML",
        reply_markup=network_ops_menu(),
    )


@router.callback_query(BulkCb.filter(F.action == "import"))
async def cb_bulk_import_compat(callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(ImportBots.waiting_tokens)
    await callback.message.edit_text(
        "📥 <b>Массовый импорт ботов</b>\n\nОтправьте токены — по одному на строке:",
        parse_mode="HTML",
    )

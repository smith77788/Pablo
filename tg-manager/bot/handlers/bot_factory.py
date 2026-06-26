"""Bot Factory — расширенный менеджер ботов с wizard-потоками."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BotCb, BotFactCb, EcoPickCb
from bot.states import BotCloneSettingsFSM, BotCreateFSM, BotTokenImportFSM, BotValidateFSM
from database import db
from services import bot_api

log = logging.getLogger(__name__)
router = Router()

# ── Helpers ───────────────────────────────────────────────────────────────

CLONE_FIELDS = [
    ("name", "Имя бота"),
    ("desc", "Описание"),
    ("short_desc", "Короткое описание"),
    ("commands", "Команды"),
]

PAGE_SIZE = 8


def _safe(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bot_label(row: asyncpg.Record) -> str:
    return f"@{row['username']}" if row.get("username") else row["first_name"]


def _parse_tokens(raw: str) -> list[str]:
    return [t.strip() for t in raw.replace(",", "\n").splitlines() if t.strip()]


async def _validate_token(http: aiohttp.ClientSession, token: str) -> dict | None:
    """Return getMe result dict on success, None if invalid, 'blocked' if 403."""
    try:
        async with http.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 403:
                return {"_blocked": True, "token": token}
            data = await resp.json()
            if data.get("ok"):
                result = data["result"]
                result["token"] = token
                return result
            return None
    except Exception:
        return None


# ── Main menu ─────────────────────────────────────────────────────────────


def _factory_menu_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать бота", callback_data=BotFactCb(action="create"))
    kb.button(text="📥 Импорт токенов", callback_data=BotFactCb(action="import_tokens"))
    kb.button(text="✅ Валидация токенов", callback_data=BotFactCb(action="validate"))
    kb.button(text="🔄 Клонировать настройки", callback_data=BotFactCb(action="clone"))
    kb.button(text="📊 Статистика ботов", callback_data=BotFactCb(action="stats"))
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="main"))
    kb.adjust(2, 2, 2)
    return kb.as_markup()


@router.callback_query(BotFactCb.filter(F.action == "menu"))
async def cb_factory_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "🤖 <b>Bot Factory</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=_factory_menu_kb(),
    )


# ── 1. Создать бота через BotFather — FSM wizard ─────────────────────────


@router.callback_query(BotFactCb.filter(F.action == "create"))
async def cb_factory_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Step 1: выбор аккаунта для управления BotFather."""
    await callback.answer()
    from bot.utils.op_helpers import _get_active_accounts, _acc_label

    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для создания ботов через BotFather нужен хотя бы один активный "
            "Telegram-аккаунт.\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    await state.set_state(BotCreateFSM.choosing_account)
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=f"✅ {_acc_label(acc)}",
            callback_data=BotFactCb(action="create_acc", bot_id=acc["id"]),
        )
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🤖 <b>Создание ботов через BotFather</b>\n\n"
        "Шаг 1: Выберите Telegram-аккаунт, который будет писать в @BotFather:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BotFactCb.filter(F.action == "create_acc"))
async def cb_factory_create_acc(
    callback: CallbackQuery,
    callback_data: BotFactCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Step 2: аккаунт выбран → спросить количество ботов."""
    from bot.utils.op_helpers import _acc_label

    try:
        acc = await pool.fetchrow(
            "SELECT id, phone, first_name, username FROM tg_accounts "
            "WHERE id=$1 AND owner_id=$2",
            callback_data.bot_id,
            callback.from_user.id,
        )
    except Exception:
        acc = None
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(acc_id=acc["id"], acc_label=_acc_label(acc))
    await state.set_state(BotCreateFSM.waiting_count)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    await callback.message.edit_text(
        f"🤖 <b>Создание ботов</b>\n\n"
        f"Аккаунт: <b>{_safe(_acc_label(acc))}</b>\n\n"
        "Шаг 2: Сколько ботов создать? (1–10):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BotCreateFSM.waiting_count, F.text)
async def fsm_botcreate_count(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 10):
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
        await message.answer("⚠️ Введите число от 1 до 10:", reply_markup=kb.as_markup())
        return
    await state.update_data(count=int(raw))
    await state.set_state(BotCreateFSM.waiting_name_tpl)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    await message.answer(
        "📝 <b>Шаблон имени</b>\n\n"
        "Введите базовое имя бота. При создании нескольких ботов к нему "
        "будет добавлен номер.\n\n"
        "Пример: <code>My Assistant</code> → <i>My Assistant 1, My Assistant 2...</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BotCreateFSM.waiting_name_tpl, F.text)
async def fsm_botcreate_name_tpl(message: Message, state: FSMContext) -> None:
    name_tpl = (message.text or "").strip()
    if not name_tpl or len(name_tpl) > 64:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
        await message.answer(
            "⚠️ Имя от 1 до 64 символов. Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(name_template=name_tpl)
    await state.set_state(BotCreateFSM.waiting_uname_tpl)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="⏭ Авто-username", callback_data=BotFactCb(action="create_skip_uname")
    )
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "🔤 <b>Шаблон username</b>\n\n"
        "Введите базовый username (без @, без суффикса <code>bot</code>).\n"
        "Суффикс <code>bot</code> добавляется автоматически.\n\n"
        "Пример: <code>myassistant</code> → <i>myassistant1bot, myassistant2bot...</i>\n\n"
        "Или нажмите «Авто-username» — система сгенерирует уникальные имена сама.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BotFactCb.filter(F.action == "create_skip_uname"))
async def cb_botcreate_skip_uname(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(uname_template="")
    await _show_botcreate_confirm(callback, state)


@router.message(BotCreateFSM.waiting_uname_tpl, F.text)
async def fsm_botcreate_uname_tpl(message: Message, state: FSMContext) -> None:
    uname = (message.text or "").strip().lstrip("@").rstrip("_")
    if uname and (len(uname) < 3 or len(uname) > 20):
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
        await message.answer(
            "⚠️ Шаблон username от 3 до 20 символов. Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(uname_template=uname)
    await _show_botcreate_confirm(message, state)


async def _show_botcreate_confirm(event, state: FSMContext) -> None:
    await state.set_state(BotCreateFSM.confirming)
    data = await state.get_data()
    count = data.get("count", 1)
    name_tpl = _safe(data.get("name_template", "Bot"))
    uname_tpl = data.get("uname_template", "")
    acc_label = _safe(data.get("acc_label", ""))

    uname_preview = (
        f"<code>{uname_tpl}1bot</code>, <code>{uname_tpl}2bot</code>..."
        if uname_tpl
        else "<i>авто-генерация</i>"
    )
    name_preview = (
        f"<code>{name_tpl} 1</code>, <code>{name_tpl} 2</code>..."
        if count > 1
        else f"<code>{name_tpl}</code>"
    )
    est_min = round(count * 2.5)

    text = (
        f"🤖 <b>Создание ботов — подтверждение</b>\n\n"
        f"Аккаунт: <b>{acc_label}</b>\n"
        f"Ботов: <b>{count}</b>\n"
        f"Имена: {name_preview}\n"
        f"Username: {uname_preview}\n\n"
        f"⏱ Ориентировочно: ~{est_min} мин\n\n"
        "Процесс выполняется в фоне. Вы получите уведомление по завершении."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=BotFactCb(action="do_create_bots"))
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    kb.adjust(2)
    markup = kb.as_markup()
    if hasattr(event, "message"):
        try:
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(BotFactCb.filter(F.action == "do_create_bots"))
async def cb_factory_do_create_bots(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Submit bot_factory operation to queue."""
    await callback.answer("⏳ Ставлю в очередь...")
    data = await state.get_data()
    await state.clear()

    acc_id = data.get("acc_id")
    count = data.get("count", 1)
    name_tpl = data.get("name_template", "Bot")
    uname_tpl = data.get("uname_template", "")

    if not acc_id:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
        await callback.message.edit_text(
            "⚠️ Сессия истекла. Начните заново.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    from bot.utils.subscription import require_plan

    if not await require_plan(pool, callback.from_user.id, "pro"):
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
        await callback.message.edit_text(
            "🔒 <b>Bot Factory — 💎 ПОДПИСКА</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    from services import operation_bus

    op_id = await operation_bus.submit(
        pool,
        callback.from_user.id,
        "bot_factory",
        {
            "acc_id": acc_id,
            "count": count,
            "name_template": name_tpl,
            "uname_template": uname_tpl,
        },
        total_items=count,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
    await callback.message.edit_text(
        f"🤖 <b>Создание ботов поставлено в очередь</b>\n\n"
        f"Ботов: <b>{count}</b>\n"
        f"Шаблон имени: <b>{_safe(name_tpl)}</b>\n"
        f"ID операции: <code>#{op_id}</code>\n\n"
        f"Боты будут созданы в фоне. Вы получите уведомление по завершении.\n"
        f"<i>Статус: /ops → 📋 Очередь</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── 2. Импорт токенов ─────────────────────────────────────────────────────


@router.callback_query(BotFactCb.filter(F.action == "import_tokens"))
async def cb_factory_import(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BotTokenImportFSM.waiting_tokens)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    await callback.message.edit_text(
        "📥 <b>Импорт токенов</b>\n\n"
        "Вставьте токены ботов — по одному на строку или через запятую.\n\n"
        "Формат: <code>123456789:AABBccDDee...</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BotTokenImportFSM.waiting_tokens, F.text)
async def msg_import_tokens(
    message: Message, state: FSMContext, http: aiohttp.ClientSession, pool: asyncpg.Pool
) -> None:
    tokens = _parse_tokens(message.text)
    if not tokens:
        await message.answer(
            "❌ Токены не найдены. Пожалуйста, отправьте токены в формате:\n"
            "<code>123456789:AABBcc...</code>",
            parse_mode="HTML",
        )
        return

    info_msg = await message.answer(f"⏳ Проверяю {len(tokens)} токен(ов)...")

    valid: list[dict] = []
    invalid: list[str] = []

    tasks = [_validate_token(http, t) for t in tokens]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for token, result in zip(tokens, results):
        if isinstance(result, Exception) or result is None:
            invalid.append(token)
        elif isinstance(result, dict) and result.get("_blocked"):
            invalid.append(token)
        else:
            valid.append(result)

    await state.update_data(valid_bots=valid, user_id=message.from_user.id)
    await state.set_state(BotTokenImportFSM.reviewing)

    # Build report text
    lines = [f"📥 <b>Результаты импорта ({len(tokens)} токенов)</b>\n"]
    if valid:
        lines.append(f"✅ <b>Успешно: {len(valid)}</b>")
        for b in valid:
            uname = (
                f"@{b['username']}" if b.get("username") else b.get("first_name", "?")
            )
            lines.append(f"• {_safe(uname)} (<code>{b['id']}</code>)")
    if invalid:
        lines.append(f"\n❌ <b>Неверные: {len(invalid)}</b>")
        for t in invalid:
            lines.append(f"• <code>{_safe(t[:40])}</code>")

    kb = InlineKeyboardBuilder()
    if valid:
        kb.button(
            text="💾 Сохранить успешные", callback_data=BotFactCb(action="import_save")
        )
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="import_cancel"))
    kb.adjust(1)

    await info_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BotFactCb.filter(F.action == "import_save"))
async def cb_import_save(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool, http: aiohttp.ClientSession
) -> None:
    await callback.answer()
    data = await state.get_data()
    valid_bots: list[dict] = data.get("valid_bots", [])
    user_id: int = data.get("user_id", callback.from_user.id)

    saved = 0
    skipped = 0
    saved_bots: list[dict] = []
    for bot_info in valid_bots:
        ok = await db.add_bot(
            pool,
            token=bot_info["token"],
            bot_id=bot_info["id"],
            username=bot_info.get("username", ""),
            first_name=bot_info.get("first_name", ""),
            added_by=user_id,
            bot=callback.bot,
        )
        if ok:
            saved += 1
            saved_bots.append(bot_info)
        else:
            skipped += 1

    await state.clear()

    # Configure newly saved bots via Bot API: delete any stale webhook so the
    # bot works in polling/long-polling mode used by BotMother.  Also apply a
    # default command list so BotFather's menu is populated.
    _DEFAULT_COMMANDS = [
        {"command": "start", "description": "Запустить бота"},
        {"command": "help", "description": "Помощь"},
    ]
    for bot_info in saved_bots:
        token = bot_info.get("token", "")
        if not token:
            continue
        try:
            await bot_api.delete_webhook(http, token)
        except Exception:
            log.debug("import_save: delete_webhook failed for bot %s", bot_info.get("id"))
        try:
            await bot_api.set_my_commands(http, token, _DEFAULT_COMMANDS)
        except Exception:
            log.debug("import_save: set_my_commands failed for bot %s", bot_info.get("id"))

    # EPOCH III: add saved bots to most recent active ecosystem
    eco_added = 0
    if saved > 0:
        try:
            from services import ecosystem_brain as _eb

            ecos = await _eb.list_ecosystems(pool, user_id)
            if ecos:
                eco_id = ecos[0]["id"]
                for bot_info in saved_bots:
                    ok = await _eb.add_member(
                        pool, eco_id, user_id, "bot", bot_info["id"]
                    )
                    if ok:
                        eco_added += 1
        except Exception:
            log.debug("import_save: ecosystem auto-add failed", exc_info=True)

    kb = InlineKeyboardBuilder()
    eco_note = f"\nДобавлено в экосистему: <b>{eco_added}</b>" if eco_added else ""
    # If at least one bot was saved, offer to add to ecosystem
    first_saved_id = 0
    if saved > 0:
        for b in valid_bots:
            if b.get("id"):
                first_saved_id = b["id"]
                break
    if first_saved_id:
        kb.button(
            text="🌐 Добавить в экосистему",
            callback_data=EcoPickCb(
                action="list", object_type="bot", object_id=first_saved_id
            ),
        )
    kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Импорт завершён</b>\n\n"
        f"Сохранено: <b>{saved}</b>\n"
        f"Пропущено (уже существуют): <b>{skipped}</b>" + eco_note,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BotFactCb.filter(F.action == "import_cancel"))
async def cb_import_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(
        "🤖 <b>Bot Factory</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=_factory_menu_kb(),
    )


# ── 3. Валидация токенов ──────────────────────────────────────────────────


@router.callback_query(BotFactCb.filter(F.action == "validate"))
async def cb_factory_validate(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BotValidateFSM.waiting_tokens)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    await callback.message.edit_text(
        "✅ <b>Валидация токенов</b>\n\n"
        "Вставьте токены для проверки — по одному на строку.\n\n"
        "Формат: <code>123456789:AABBccDDee...</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BotValidateFSM.waiting_tokens, F.text)
async def msg_validate_tokens(
    message: Message, state: FSMContext, http: aiohttp.ClientSession
) -> None:
    tokens = _parse_tokens(message.text)
    if not tokens:
        await message.answer(
            "❌ Токены не найдены. Пожалуйста, отправьте токены в формате:\n"
            "<code>123456789:AABBcc...</code>",
            parse_mode="HTML",
        )
        return

    info_msg = await message.answer(f"⏳ Проверяю {len(tokens)} токен(ов)...")

    working: list[dict] = []
    broken: list[str] = []
    blocked: list[str] = []

    tasks = [_validate_token(http, t) for t in tokens]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for token, result in zip(tokens, results):
        if isinstance(result, Exception) or result is None:
            broken.append(token)
        elif isinstance(result, dict) and result.get("_blocked"):
            blocked.append(token)
        else:
            working.append(result)

    await state.clear()

    lines = [f"<b>Результаты проверки ({len(tokens)} токенов)</b>\n"]
    lines.append(f"✅ <b>Рабочие: {len(working)} токенов</b>")
    for b in working:
        uname = f"@{b['username']}" if b.get("username") else b.get("first_name", "?")
        lines.append(f"  • {_safe(uname)} (<code>{b['id']}</code>)")
    lines.append(f"\n❌ <b>Нерабочие: {len(broken)} токенов</b>")
    for t in broken:
        lines.append(f"  • <code>{_safe(t[:40])}</code>")
    lines.append(f"\n⚠️ <b>Заблокированные: {len(blocked)} токенов</b>")
    for t in blocked:
        lines.append(f"  • <code>{_safe(t[:40])}</code>")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
    await info_msg.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── 4. Клонировать настройки ──────────────────────────────────────────────


def _bots_source_kb(bots: list[asyncpg.Record], page: int = 0) -> object:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    chunk = bots[start : start + PAGE_SIZE]
    for row in chunk:
        label = _bot_label(row)
        kb.button(
            text=label,
            callback_data=BotFactCb(action="clone_src", bot_id=row["bot_id"]),
        )
    kb.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(
            text="◀️", callback_data=BotFactCb(action="clone_src_page", page=page - 1)
        )
    if start + PAGE_SIZE < len(bots):
        nav.button(
            text="▶️", callback_data=BotFactCb(action="clone_src_page", page=page + 1)
        )
    nav.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    nav.adjust(2, 1)
    kb.attach(nav)
    return kb.as_markup()


def _fields_kb(selected: set[str]) -> object:
    kb = InlineKeyboardBuilder()
    for key, label in CLONE_FIELDS:
        icon = "☑️" if key in selected else "☐"
        kb.button(
            text=f"{icon} {label}",
            callback_data=BotFactCb(action=f"clone_field_{key}"),
        )
    kb.button(text="✅ Продолжить", callback_data=BotFactCb(action="clone_fields_done"))
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def _bots_targets_kb(
    bots: list[asyncpg.Record], selected: set[int], src_bot_id: int, page: int = 0
) -> object:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    chunk = bots[start : start + PAGE_SIZE]
    for row in chunk:
        if row["bot_id"] == src_bot_id:
            continue
        label = _bot_label(row)
        icon = "☑️" if row["bot_id"] in selected else "☐"
        kb.button(
            text=f"{icon} {label}",
            callback_data=BotFactCb(action="clone_tgt", bot_id=row["bot_id"]),
        )
    kb.adjust(2)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(
            text="◀️", callback_data=BotFactCb(action="clone_tgt_page", page=page - 1)
        )
    if start + PAGE_SIZE < len(bots):
        nav.button(
            text="▶️", callback_data=BotFactCb(action="clone_tgt_page", page=page + 1)
        )
    nav.button(
        text="✅ Продолжить", callback_data=BotFactCb(action="clone_targets_done")
    )
    nav.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    nav.adjust(2, 1, 1)
    kb.attach(nav)
    return kb.as_markup()


@router.callback_query(BotFactCb.filter(F.action == "clone"))
async def cb_factory_clone(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    bots = await db.get_bots(pool, callback.from_user.id)
    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
        await callback.message.edit_text(
            "❌ У вас нет добавленных ботов.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    await state.set_state(BotCloneSettingsFSM.choosing_source)
    await state.update_data(bots_cache=[dict(r) for r in bots])
    await callback.message.edit_text(
        "🔄 <b>Клонировать настройки</b>\n\nШаг 1: Выберите <b>источник</b> — бота, настройки которого хотите скопировать:",
        parse_mode="HTML",
        reply_markup=_bots_source_kb(bots, page=0),
    )


@router.callback_query(BotFactCb.filter(F.action == "clone_src_page"))
async def cb_clone_src_page(
    callback: CallbackQuery,
    callback_data: BotFactCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    bots = await db.get_bots(pool, callback.from_user.id)
    await callback.message.edit_reply_markup(
        reply_markup=_bots_source_kb(bots, page=callback_data.page)
    )


@router.callback_query(BotFactCb.filter(F.action == "clone_src"))
async def cb_clone_src(
    callback: CallbackQuery, callback_data: BotFactCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(
        src_bot_id=callback_data.bot_id,
        selected_fields=list(k for k, _ in CLONE_FIELDS),  # all selected by default
        selected_targets=[],
    )
    await state.set_state(BotCloneSettingsFSM.choosing_fields)

    selected = set(k for k, _ in CLONE_FIELDS)
    await callback.message.edit_text(
        "🔄 <b>Клонировать настройки</b>\n\nШаг 2: Выберите <b>поля</b> для клонирования:",
        parse_mode="HTML",
        reply_markup=_fields_kb(selected),
    )


@router.callback_query(BotFactCb.filter(F.action.startswith("clone_field_")))
async def cb_clone_field_toggle(
    callback: CallbackQuery, callback_data: BotFactCb, state: FSMContext
) -> None:
    await callback.answer()
    # Extract field key from action e.g. "clone_field_name" -> "name"
    field_key = callback_data.action[len("clone_field_") :]
    data = await state.get_data()
    selected: set[str] = set(data.get("selected_fields", []))
    if field_key in selected:
        selected.discard(field_key)
    else:
        selected.add(field_key)
    await state.update_data(selected_fields=list(selected))
    await callback.message.edit_reply_markup(reply_markup=_fields_kb(selected))


@router.callback_query(BotFactCb.filter(F.action == "clone_fields_done"))
async def cb_clone_fields_done(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    if not data.get("selected_fields"):
        await callback.answer("Выберите хотя бы одно поле!", show_alert=True)
        return
    await callback.answer()

    bots = await db.get_bots(pool, callback.from_user.id)
    src_bot_id = data.get("src_bot_id", 0)
    selected_targets: set[int] = set(data.get("selected_targets", []))
    await state.set_state(BotCloneSettingsFSM.choosing_targets)
    await callback.message.edit_text(
        "🔄 <b>Клонировать настройки</b>\n\nШаг 3: Выберите <b>целевые боты</b> (в которые скопировать настройки):",
        parse_mode="HTML",
        reply_markup=_bots_targets_kb(bots, selected_targets, src_bot_id, page=0),
    )


@router.callback_query(BotFactCb.filter(F.action == "clone_tgt_page"))
async def cb_clone_tgt_page(
    callback: CallbackQuery,
    callback_data: BotFactCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    data = await state.get_data()
    bots = await db.get_bots(pool, callback.from_user.id)
    src_bot_id = data.get("src_bot_id", 0)
    selected_targets: set[int] = set(data.get("selected_targets", []))
    await callback.message.edit_reply_markup(
        reply_markup=_bots_targets_kb(
            bots, selected_targets, src_bot_id, page=callback_data.page
        )
    )


@router.callback_query(BotFactCb.filter(F.action == "clone_tgt"))
async def cb_clone_tgt(
    callback: CallbackQuery,
    callback_data: BotFactCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    data = await state.get_data()
    selected_targets: set[int] = set(data.get("selected_targets", []))
    tgt_id = callback_data.bot_id
    if tgt_id in selected_targets:
        selected_targets.discard(tgt_id)
    else:
        selected_targets.add(tgt_id)
    await state.update_data(selected_targets=list(selected_targets))

    bots = await db.get_bots(pool, callback.from_user.id)
    src_bot_id = data.get("src_bot_id", 0)
    await callback.message.edit_reply_markup(
        reply_markup=_bots_targets_kb(bots, selected_targets, src_bot_id, page=0)
    )


@router.callback_query(BotFactCb.filter(F.action == "clone_targets_done"))
async def cb_clone_targets_done(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    selected_targets: list[int] = data.get("selected_targets", [])
    if not selected_targets:
        await callback.answer("Выберите хотя бы одного целевого бота!", show_alert=True)
        return
    await callback.answer()

    src_bot_id = data.get("src_bot_id", 0)
    selected_fields: list[str] = data.get("selected_fields", [])
    bots = await db.get_bots(pool, callback.from_user.id)

    bot_map: dict[int, asyncpg.Record] = {r["bot_id"]: r for r in bots}
    src_row = bot_map.get(src_bot_id)
    src_label = _safe(_bot_label(src_row)) if src_row else str(src_bot_id)

    field_labels = [label for key, label in CLONE_FIELDS if key in selected_fields]
    tgt_labels = [
        _safe(_bot_label(bot_map[tid])) for tid in selected_targets if tid in bot_map
    ]

    await state.set_state(BotCloneSettingsFSM.confirming)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Выполнить", callback_data=BotFactCb(action="clone_confirm"))
    kb.button(text="❌ Отмена", callback_data=BotFactCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🔄 <b>Клонировать настройки</b>\n\n"
        f"Источник: <b>{src_label}</b>\n"
        f"Поля: <b>{', '.join(field_labels)}</b>\n"
        f"Цели: <b>{', '.join(tgt_labels)}</b>\n\n"
        "Подтвердить операцию?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(BotFactCb.filter(F.action == "clone_confirm"))
async def cb_clone_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    await callback.answer()
    data = await state.get_data()
    src_bot_id: int = data.get("src_bot_id", 0)
    selected_targets: list[int] = data.get("selected_targets", [])
    selected_fields: list[str] = data.get("selected_fields", [])

    # Fetch source bot token
    src_row = await db.get_bot(pool, src_bot_id, callback.from_user.id)
    if not src_row:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BotFactCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            "❌ Источник не найден.", reply_markup=kb.as_markup()
        )
        await state.clear()
        return

    src_token = src_row["token"]

    # Fetch source settings
    src_name = ""
    src_desc = ""
    src_short = ""
    src_commands: list[dict] = []

    if "name" in selected_fields:
        src_name = await bot_api.get_my_name(http, src_token)
    if "desc" in selected_fields:
        src_desc = await bot_api.get_my_description(http, src_token)
    if "short_desc" in selected_fields:
        src_short = await bot_api.get_my_short_description(http, src_token)
    if "commands" in selected_fields:
        src_commands = await bot_api.get_my_commands(http, src_token)

    ok_count = 0
    fail_count = 0

    for tgt_id in selected_targets:
        tgt_row = await db.get_bot(pool, tgt_id, callback.from_user.id)
        if not tgt_row:
            fail_count += 1
            continue
        tgt_token = tgt_row["token"]
        try:
            if "name" in selected_fields and src_name:
                await bot_api.set_name(http, tgt_token, src_name)
                await asyncio.sleep(1)
            if "desc" in selected_fields and src_desc:
                await bot_api.set_description(http, tgt_token, src_desc)
                await asyncio.sleep(1)
            if "short_desc" in selected_fields and src_short:
                await bot_api.set_short_description(http, tgt_token, src_short)
                await asyncio.sleep(1)
            if "commands" in selected_fields and src_commands:
                await bot_api.set_my_commands(http, tgt_token, src_commands)
                await asyncio.sleep(1)
            ok_count += 1
        except Exception as exc:
            log.warning("clone_confirm error for bot %s: %s", tgt_id, exc)
            fail_count += 1

    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
    await callback.message.edit_text(
        f"✅ <b>Клонирование завершено</b>\n\n"
        f"Скопировано в: <b>{ok_count}</b> бот(ов)\n"
        f"Ошибок: <b>{fail_count}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── 5. Статистика ботов ───────────────────────────────────────────────────


@router.callback_query(BotFactCb.filter(F.action == "stats"))
async def cb_factory_stats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    user_id = callback.from_user.id

    try:
        row = await pool.fetchrow(
            """
            SELECT
                COUNT(*) AS total_bots,
                COUNT(CASE WHEN is_active THEN 1 END) AS active_bots,
                SUM(COALESCE(
                    (SELECT COUNT(*) FROM bot_users WHERE bot_id = bots.id), 0
                )) AS total_users
            FROM managed_bots bots
            WHERE owner_id = $1
            """,
            user_id,
        )
        # Fallback query using added_by if owner_id column doesn't exist
        if row is None:
            row = await pool.fetchrow(
                """
                SELECT
                    COUNT(*) AS total_bots,
                    COUNT(CASE WHEN is_active THEN 1 END) AS active_bots,
                    COALESCE(SUM(
                        (SELECT COUNT(*) FROM bot_users bu WHERE bu.bot_id = mb.bot_id)
                    ), 0) AS total_users
                FROM managed_bots mb
                WHERE mb.added_by = $1
                """,
                user_id,
            )
    except Exception as exc:
        log.warning("cb_factory_stats: DB error: %s", exc)
        row = None

    if row is None:
        total_bots = active_bots = total_users = 0
    else:
        total_bots = row["total_bots"] or 0
        active_bots = row["active_bots"] or 0
        total_users = row["total_users"] or 0

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Bot Factory", callback_data=BotFactCb(action="menu"))
    await callback.message.edit_text(
        f"📊 <b>Статистика ботов</b>\n\n"
        f"Всего ботов: <b>{total_bots}</b>\n"
        f"Активных: <b>{active_bots}</b>\n"
        f"Всего пользователей: <b>{total_users:,}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

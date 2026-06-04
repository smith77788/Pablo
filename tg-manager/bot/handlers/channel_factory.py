"""Channel Factory — extended channel manager.

Provides:
  - Single channel creation with username / cluster / description
  - Bulk channel creation (numbered prefix, anti-flood)
  - Bulk channel editing (title / description across all or by account)
  - Invite link generation per channel
  - Channel stats (account → channel list → member count)
  - Import existing Telegram channels into the system

Entry point: ChanFactCb(action="menu")
"""

from __future__ import annotations

import asyncio
import html
import logging
import random

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AccCb, ChanFactCb, SeoCb, EcoPickCb
from services.logger import log_exc_swallow
from bot.states import (
    BulkChannelCreateFSM,
    ChannelFactoryFSM,
    EditChannelBulkFSM,
)
from bot.utils.op_helpers import (
    _acc_label,
    _get_active_accounts,
    _progress_text,
    backoff,
)
from services import session_simulator


log = logging.getLogger(__name__)
router = Router()

_PRO = "pro"


# ── Helpers ────────────────────────────────────────────────────────────────


def _back_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    return kb


def _no_accounts_kb() -> InlineKeyboardBuilder:
    """Клавиатура для экранов 'нет активных аккаунтов'."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Перейти к аккаунтам", callback_data=AccCb(action="menu"))
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    return kb


async def _send_or_edit(event, text: str, kb: InlineKeyboardBuilder) -> None:
    markup = kb.as_markup()
    if hasattr(event, "message"):
        try:
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            log_exc_swallow(log, "Не удалось отредактировать сообщение в _send_or_edit")
        await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=markup)


# ── Main menu ──────────────────────────────────────────────────────────────


def _main_menu_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Создать канал", callback_data=ChanFactCb(action="create"))
    kb.button(
        text="📋 Массовое создание", callback_data=ChanFactCb(action="bulk_create")
    )
    kb.button(text="📥 Импорт из Telegram", callback_data=ChanFactCb(action="import"))
    kb.button(text="✏️ Редактировать", callback_data=ChanFactCb(action="bulk_edit"))
    kb.button(
        text="📤 Массовая публикация",
        callback_data=ChanFactCb(action="mass_pub_redirect"),
    )
    kb.button(text="📊 Статистика каналов", callback_data=ChanFactCb(action="stats"))
    kb.button(text="📈 SEO-оптимизация", callback_data=ChanFactCb(action="seo_pick"))
    kb.button(text="🔗 Генерация ссылок", callback_data=ChanFactCb(action="gen_links"))
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="back_to_ops"))
    kb.adjust(2, 2, 2, 2, 1)
    return kb


@router.callback_query(ChanFactCb.filter(F.action == "menu"))
async def cb_chanf_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "📡 <b>Channel Factory — менеджер каналов</b>\n\n"
        "• <b>Создать канал</b> — новый Telegram-канал через ваш аккаунт\n"
        "• <b>Массовое создание</b> — несколько каналов с умными задержками\n"
        "• <b>Импорт из Telegram</b> — подключить уже существующие каналы\n"
        "• <b>Редактировать</b> — массово изменить название/описание\n"
        "• <b>Массовая публикация</b> — опубликовать пост во все каналы\n"
        "• <b>Статистика</b> — подписчики, активность\n"
        "• <b>Генерация ссылок</b> — invite-ссылки для каналов",
        parse_mode="HTML",
        reply_markup=_main_menu_kb().as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "back_to_ops"))
async def cb_chanf_back_ops(callback: CallbackQuery) -> None:
    """Redirect back to the channel ops main menu."""
    await callback.answer()
    from bot.callbacks import ChanCb

    kb = InlineKeyboardBuilder()
    kb.button(text="📡 Перейти в операции", callback_data=ChanCb(action="menu"))
    await callback.message.edit_text(
        "◀️ Вернитесь в главное меню операций через /ops",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# IMPORT EXISTING CHANNELS — подключить уже существующие каналы
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanFactCb.filter(F.action == "import"))
async def cb_chanf_import(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Step 1: выбор аккаунта для импорта каналов."""
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для импорта каналов нужен хотя бы один активный Telegram-аккаунт.\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=_acc_label(acc),
            callback_data=ChanFactCb(action="import_acc", acc_id=acc["id"]),
        )
    kb.button(
        text="🔄 Все аккаунты сразу", callback_data=ChanFactCb(action="import_all_accs")
    )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(2, 1, 1)
    await callback.message.edit_text(
        "📥 <b>Импорт существующих каналов</b>\n\n"
        "Мы загрузим список каналов из выбранного аккаунта "
        "и подключим их к системе. После этого вы сможете:\n"
        "• публиковать посты через «Массовая публикация»\n"
        "• делать invite и операции с участниками\n"
        "• видеть статистику\n\n"
        "Выберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "import_acc"))
async def cb_chanf_import_acc(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    """Step 2: загрузить каналы аккаунта и сохранить в систему."""
    acc = await pool.fetchrow(
        "SELECT id, session_str, phone, first_name, username, "
        "device_model, system_version, app_version FROM tg_accounts "
        "WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю каналы из Telegram...")

    from services import account_manager
    from database.db import upsert_managed_channels

    try:
        dialogs = (
            await account_manager.get_dialogs(acc["session_str"], limit=200, _acc=acc)
            or []
        )
    except Exception as e:
        log.warning("import_acc get_dialogs error: %s", e)
        await callback.message.edit_text(
            f"❌ Ошибка при получении диалогов: <code>{html.escape(str(e)[:100])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    channels = [
        d
        for d in dialogs
        if d.get("type") in ("channel", "megagroup", "supergroup", "gigagroup")
    ]
    if not channels:
        await callback.message.edit_text(
            "ℹ️ У этого аккаунта нет каналов или супергрупп в Telegram.\n\n"
            "Убедитесь что аккаунт является администратором нужных каналов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    await upsert_managed_channels(pool, callback.from_user.id, acc["id"], channels)

    acc_label = _acc_label(acc)
    lines = [f"📥 <b>Импортировано каналов: {len(channels)}</b>\n"]
    lines += [
        f"• {html.escape(ch.get('title', '(без названия)'))}"
        + (f" @{html.escape(ch['username'])}" if ch.get("username") else "")
        for ch in channels[:20]
    ]
    if len(channels) > 20:
        lines.append(f"... и ещё {len(channels) - 20}")
    lines.append(f"\n<i>Аккаунт: {html.escape(acc_label)}</i>")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📤 Открыть публикацию",
        callback_data=ChanFactCb(action="mass_pub_redirect"),
    )
    kb.button(text="◀️ В меню каналов", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "import_all_accs"))
async def cb_chanf_import_all_accs(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Импортировать каналы со ВСЕХ активных аккаунтов."""
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.answer("Нет активных аккаунтов.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю каналы со всех аккаунтов...")

    from services import account_manager
    from database.db import upsert_managed_channels

    total_imported = 0
    errors = []
    progress_msg = await callback.message.edit_text(
        f"⏳ Обработка аккаунтов: 0/{len(accounts)}...",
        parse_mode="HTML",
    )

    for idx, acc in enumerate(accounts):
        try:
            dialogs = (
                await account_manager.get_dialogs(
                    acc["session_str"], limit=200, _acc=acc
                )
                or []
            )
            channels = [
                d
                for d in dialogs
                if d.get("type") in ("channel", "megagroup", "supergroup", "gigagroup")
            ]
            if channels:
                await upsert_managed_channels(
                    pool, callback.from_user.id, acc["id"], channels
                )
                total_imported += len(channels)
            await progress_msg.edit_text(
                f"⏳ Обработка аккаунтов: {idx + 1}/{len(accounts)}...\n"
                f"Найдено каналов: {total_imported}",
                parse_mode="HTML",
            )
            if idx < len(accounts) - 1:
                await session_simulator.short_pause(2.0, 5.0)
        except Exception as e:
            log.warning("import_all_accs acc=%s error: %s", acc.get("id"), e)
            errors.append(f"• {_acc_label(acc)}: {str(e)[:50]}")

    text = f"✅ <b>Импорт завершён</b>\n\nПодключено каналов: <b>{total_imported}</b>"
    if errors:
        text += f"\n\n⚠️ Ошибки ({len(errors)}):\n" + "\n".join(errors[:5])
    text += "\n\nТеперь вы можете использовать эти каналы для публикации и операций."

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📤 Открыть публикацию",
        callback_data=ChanFactCb(action="mass_pub_redirect"),
    )
    kb.button(text="◀️ В меню каналов", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    await progress_msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanFactCb.filter(F.action == "mass_pub_redirect"))
async def cb_chanf_mass_pub_redirect(callback: CallbackQuery) -> None:
    """Redirect user to mass publish wizard."""
    await callback.answer()
    from bot.callbacks import MassPubCb

    kb = InlineKeyboardBuilder()
    kb.button(text="📤 Открыть Mass Publish", callback_data=MassPubCb(action="menu"))
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📤 <b>Массовая публикация</b>\n\n"
        "Для массовой публикации используйте отдельный модуль Mass Publish.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# 1. CREATE SINGLE CHANNEL  (FSM: ChannelFactoryFSM)
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanFactCb.filter(F.action == "create"))
async def cb_chanf_create_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    from bot.utils.subscription import require_plan

    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Создание каналов — PRO</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для создания канала нужен хотя бы один активный Telegram-аккаунт.\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for i, acc in enumerate(accounts):
        kb.button(
            text=_acc_label(acc),
            callback_data=ChanFactCb(action="create_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(3)
    await state.set_state(ChannelFactoryFSM.choosing_account)
    await callback.message.edit_text(
        "➕ <b>Создать канал</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "create_acc"))
async def cb_chanf_create_acc_chosen(
    callback: CallbackQuery,
    callback_data: ChanFactCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    acc = await pool.fetchrow(
        "SELECT id, phone, first_name, username, session_str "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], acc_label=_acc_label(acc))

    sd = await state.get_data()
    prefill = sd.get("tpl_prefill") or {}
    if prefill.get("title"):
        await state.update_data(
            title=prefill.get("title", ""),
            about=prefill.get("description") or prefill.get("about") or "",
            channel_username=(prefill.get("username") or "").lstrip("@"),
            tpl_prefill=None,
        )
        await _show_chanf_cluster_or_confirm(callback, state, pool)
        return

    await state.set_state(ChannelFactoryFSM.waiting_title)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    await callback.message.edit_text(
        f"➕ <b>Название канала</b>\n\nАккаунт: <b>{html.escape(_acc_label(acc))}</b>\n\n"
        "Введите название канала (до 128 символов):\n\n"
        "💡 <b>Примеры:</b>\n"
        "• <code>Crypto News | BTC &amp; ETH</code>\n"
        "• <code>Мой Блог — Новости дня</code>\n"
        "• <code>Travel Tips ✈️</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ChannelFactoryFSM.waiting_title)
async def fsm_chanf_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not title or len(title) > 128:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
        await message.answer(
            "⚠️ Название от 1 до 128 символов. Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(title=title)
    await state.set_state(ChannelFactoryFSM.waiting_about)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ChanFactCb(action="skip_about"))
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "📄 <b>Описание канала</b>\n\nВведите описание (до 255 символов) или пропустите:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "skip_about"))
async def cb_chanf_skip_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(about="")
    await state.set_state(ChannelFactoryFSM.waiting_username)
    await _ask_username(callback.message, edit=True)


@router.message(ChannelFactoryFSM.waiting_about)
async def fsm_chanf_about(message: Message, state: FSMContext) -> None:
    about = (message.text or "").strip()[:255]
    await state.update_data(about=about)
    await state.set_state(ChannelFactoryFSM.waiting_username)
    await _ask_username(message, edit=False)


async def _ask_username(msg, edit: bool) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без username", callback_data=ChanFactCb(action="skip_username"))
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    text = (
        "🔤 <b>Username канала</b>\n\n"
        "Введите username (только a-z, 0-9, _, минимум 5 символов)\n"
        "или пропустите — канал будет приватным:"
    )
    if edit:
        try:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
            return
        except Exception:
            log_exc_swallow(
                log, "Не удалось отредактировать сообщение при показе username"
            )
    await msg.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanFactCb.filter(F.action == "skip_username"))
async def cb_chanf_skip_username(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await state.update_data(channel_username="")
    await _show_chanf_cluster_or_confirm(callback, state, pool)


@router.message(ChannelFactoryFSM.waiting_username)
async def fsm_chanf_username(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    uname = (message.text or "").strip().lstrip("@")
    if uname and (len(uname) < 5 or not all(c.isalnum() or c == "_" for c in uname)):
        kb = InlineKeyboardBuilder()
        kb.button(
            text="⏭ Без username", callback_data=ChanFactCb(action="skip_username")
        )
        kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
        kb.adjust(1)
        await message.answer(
            "⚠️ Username должен быть не менее 5 символов, только a-z, 0-9 и _. Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(channel_username=uname)
    await _show_chanf_cluster_or_confirm(message, state, pool)


async def _show_chanf_cluster_or_confirm(
    event, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Try to show cluster selection; if no clusters — go straight to confirm."""
    owner_id = (
        event.from_user.id
        if hasattr(event, "from_user")
        else event.message.from_user.id
    )
    clusters: list[asyncpg.Record] = []
    try:
        clusters = await pool.fetch(
            "SELECT id, name FROM clusters WHERE owner_id=$1 ORDER BY name LIMIT 20",
            owner_id,
        )
    except Exception:
        log_exc_swallow(log, "Не удалось загрузить список кластеров")

    if not clusters:
        await state.update_data(cluster_id=None, cluster_name="")
        await state.set_state(ChannelFactoryFSM.confirming)
        await _show_chanf_confirm(event, state)
        return

    await state.set_state(ChannelFactoryFSM.choosing_cluster)
    kb = InlineKeyboardBuilder()
    for cl in clusters:
        kb.button(
            text=cl["name"],
            callback_data=ChanFactCb(action="pick_cluster", channel_id=cl["id"]),
        )
    kb.button(text="— Без кластера —", callback_data=ChanFactCb(action="skip_cluster"))
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    text = "🗂 <b>Кластер</b>\n\nВыберите кластер для канала или пропустите:"
    if hasattr(event, "message"):
        try:
            await event.message.edit_text(
                text, parse_mode="HTML", reply_markup=kb.as_markup()
            )
            return
        except Exception:
            log_exc_swallow(
                log, "Не удалось отредактировать сообщение при выборе кластера"
            )
        await event.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ChanFactCb.filter(F.action == "pick_cluster"))
async def cb_chanf_pick_cluster(
    callback: CallbackQuery,
    callback_data: ChanFactCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    cl = await pool.fetchrow(
        "SELECT id, name FROM clusters WHERE id=$1", callback_data.channel_id
    )
    cluster_name = cl["name"] if cl else ""
    await state.update_data(
        cluster_id=callback_data.channel_id, cluster_name=cluster_name
    )
    await state.set_state(ChannelFactoryFSM.confirming)
    await _show_chanf_confirm(callback, state)


@router.callback_query(ChanFactCb.filter(F.action == "skip_cluster"))
async def cb_chanf_skip_cluster(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(cluster_id=None, cluster_name="")
    await state.set_state(ChannelFactoryFSM.confirming)
    await _show_chanf_confirm(callback, state)


async def _show_chanf_confirm(event, state: FSMContext) -> None:
    data = await state.get_data()
    title = html.escape(data.get("title", ""))
    about = html.escape(data.get("about", "") or "—")
    uname = data.get("channel_username", "")
    uname_s = f"@{html.escape(uname)}" if uname else "—"
    cluster = html.escape(data.get("cluster_name", "") or "—")
    acc_label = html.escape(data.get("acc_label", ""))
    text = (
        "📡 <b>Создание канала</b>\n\n"
        f"Аккаунт: <b>{acc_label}</b>\n"
        f"Название: <b>{title}</b>\n"
        f"Описание: <b>{about}</b>\n"
        f"Username: <b>{uname_s}</b>\n"
        f"Кластер: <b>{cluster}</b>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=ChanFactCb(action="do_create"))
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    kb.adjust(2)
    markup = kb.as_markup()
    if hasattr(event, "message"):
        try:
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            log_exc_swallow(
                log,
                "Не удалось отредактировать сообщение подтверждения создания канала",
            )
        await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(ChanFactCb.filter(F.action == "do_create"))
async def cb_chanf_do_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Создаю канал...")
    data = await state.get_data()
    await state.clear()

    acc_id = data.get("acc_id")
    if not acc_id:
        await callback.message.edit_text(
            "⚠️ Сессия истекла. Начните заново.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    acc = await pool.fetchrow(
        "SELECT id, session_str, device_model, system_version, app_version "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    from services import account_manager

    result = await account_manager.create_channel(
        acc["session_str"],
        title=data["title"],
        about=data.get("about", ""),
        _acc=dict(acc),
    )

    if "error" in result:
        err = html.escape(result["error"])
        await callback.message.edit_text(
            f"❌ <b>Ошибка создания</b>\n\n<code>{err}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    channel_id = result["channel_id"]
    title_s = html.escape(result["title"])
    invite = result.get("invite_link", "")

    # Set username if provided
    uname = data.get("channel_username", "")
    uname_result = ""
    if uname:
        try:
            err_u = await account_manager.set_channel_username(
                acc["session_str"], channel_id, uname, _acc=acc
            )
            uname_result = (
                f"\nUsername: @{html.escape(uname)}"
                if not err_u
                else f"\n⚠️ Username не установлен: {html.escape(err_u)}"
            )
        except Exception as e:
            uname_result = f"\n⚠️ Username не установлен: {html.escape(str(e))}"

    # Build t.me/username link if username was set successfully
    tme_link = ""
    if uname and "не установлен" not in uname_result:
        tme_link = f'\nt.me: <a href="https://t.me/{html.escape(uname)}">t.me/{html.escape(uname)}</a>'

    # EPOCH III: auto-add channel to most recent active ecosystem
    try:
        from services import ecosystem_brain as _eb

        ecos = await _eb.list_ecosystems(pool, callback.from_user.id)
        if ecos:
            await _eb.add_member(
                pool, ecos[0]["id"], callback.from_user.id, "channel", channel_id
            )
    except Exception:
        pass

    kb = InlineKeyboardBuilder()
    if uname and "не установлен" not in uname_result:
        kb.button(
            text=f"🔗 Открыть t.me/{html.escape(uname)}", url=f"https://t.me/{uname}"
        )
    kb.button(
        text="🌐 Добавить в экосистему",
        callback_data=EcoPickCb(
            action="list", object_type="channel", object_id=channel_id
        ),
    )
    kb.button(text="◀️ Меню", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Канал создан!</b>\n\n"
        f"Название: <b>{title_s}</b>\n"
        f"ID: <code>{channel_id}</code>"
        + (f"\nСсылка: {html.escape(invite)}" if invite else "")
        + uname_result
        + tme_link,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# 2. BULK CREATE  (FSM: BulkChannelCreateFSM)
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanFactCb.filter(F.action == "bulk_create"))
async def cb_chanf_bulk_create_start(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    from bot.utils.subscription import require_plan

    if not await require_plan(pool, callback.from_user.id, _PRO):
        await callback.message.edit_text(
            "🔒 <b>Массовое создание — PRO</b>\n\nОформите: /subscription",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для массового создания каналов нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=_acc_label(acc),
            callback_data=ChanFactCb(action="bulk_create_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(3)
    await state.set_state(BulkChannelCreateFSM.choosing_account)
    await callback.message.edit_text(
        "📋 <b>Массовое создание каналов</b>\n\nВыберите аккаунт:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "bulk_create_acc"))
async def cb_chanf_bulk_create_acc(
    callback: CallbackQuery,
    callback_data: ChanFactCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    acc = await pool.fetchrow(
        "SELECT id, phone, first_name, username, session_str "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(acc_id=acc["id"], acc_label=_acc_label(acc))
    await state.set_state(BulkChannelCreateFSM.waiting_count)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    await callback.message.edit_text(
        f"📋 <b>Сколько каналов создать?</b>\n\n"
        f"Аккаунт: <b>{html.escape(_acc_label(acc))}</b>\n\n"
        "Введите число от 1 до 10:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkChannelCreateFSM.waiting_count)
async def fsm_bulk_chan_count(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 10):
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
        await message.answer("⚠️ Введите число от 1 до 10:", reply_markup=kb.as_markup())
        return
    await state.update_data(channel_count=int(raw))
    await state.set_state(BulkChannelCreateFSM.waiting_prefix)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    await message.answer(
        "📝 <b>Название каналов</b>\n\n"
        "Введите базовое название. Каналы получат номер:\n"
        "<i>Название 1, Название 2...</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(BulkChannelCreateFSM.waiting_prefix)
async def fsm_bulk_chan_prefix(message: Message, state: FSMContext) -> None:
    prefix = (message.text or "").strip()
    if not prefix or len(prefix) > 100:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
        await message.answer(
            "⚠️ Название от 1 до 100 символов. Попробуйте ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(prefix=prefix)
    await state.set_state(BulkChannelCreateFSM.waiting_about)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=ChanFactCb(action="bulk_skip_about"))
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "📄 <b>Описание</b>\n\nОдно описание для всех каналов (или пропустите):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "bulk_skip_about"))
async def cb_chanf_bulk_skip_about(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(about="")
    await state.set_state(BulkChannelCreateFSM.confirming)
    await _show_bulk_confirm(callback, state)


@router.message(BulkChannelCreateFSM.waiting_about)
async def fsm_bulk_chan_about(message: Message, state: FSMContext) -> None:
    about = (message.text or "").strip()[:255]
    await state.update_data(about=about)
    await state.set_state(BulkChannelCreateFSM.confirming)
    await _show_bulk_confirm(message, state)


async def _show_bulk_confirm(event, state: FSMContext) -> None:
    data = await state.get_data()
    count = data.get("channel_count", 1)
    prefix = html.escape(data.get("prefix", ""))
    acc_label = html.escape(data.get("acc_label", ""))
    # Build preview names
    preview = ", ".join(f"'{prefix} {i}'" for i in range(1, min(count + 1, 4)))
    if count > 3:
        preview += "..."
    text = (
        f"📋 <b>Подтвердите массовое создание</b>\n\n"
        f"Аккаунт: <b>{acc_label}</b>\n"
        f"Каналов: <b>{count}</b>\n"
        f"Названия: {preview}\n\n"
        "🛡️ <b>Умный режим:</b>\n"
        "• 45-90 сек между созданиями\n"
        "• Пауза 5-10 мин каждые 5 каналов\n"
        "• Случайные задержки имитируют человека\n\n"
        "<i>⏱ Ориентировочное время: ~{est} мин</i>".format(
            est=round((count * 67 + (count // 5) * 450) / 60)
        )
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать", callback_data=ChanFactCb(action="do_bulk_create"))
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    kb.adjust(2)
    markup = kb.as_markup()
    if hasattr(event, "message"):
        try:
            await event.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            return
        except Exception:
            log_exc_swallow(
                log,
                "Не удалось отредактировать сообщение подтверждения массового создания",
            )
        await event.message.answer(text, parse_mode="HTML", reply_markup=markup)
    else:
        await event.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(ChanFactCb.filter(F.action == "do_bulk_create"))
async def cb_chanf_do_bulk_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Ставлю в очередь...")
    data = await state.get_data()
    await state.clear()

    acc_id = data.get("acc_id")
    count = data.get("channel_count", 1)
    prefix = data.get("prefix", "Channel")
    about = data.get("about", "")
    username_pattern = data.get("username_pattern", "")

    acc = await pool.fetchrow(
        "SELECT id FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.message.edit_text(
            "⚠️ Аккаунт не найден.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    from services import operation_bus

    op_id = await operation_bus.submit(
        pool,
        callback.from_user.id,
        "bulk_create_channels",
        {
            "prefix": prefix,
            "count": count,
            "about": about,
            "username_pattern": username_pattern,
            "acc_id": acc_id,
        },
        total_items=count,
    )

    await callback.message.edit_text(
        f"📡 <b>Массовое создание поставлено в очередь</b>\n\n"
        f"Аккаунт: <code>{acc_id}</code>\n"
        f"Префикс: <b>{html.escape(prefix)}</b>\n"
        f"Каналов: <b>{count}</b>\n"
        f"ID операции: <code>#{op_id}</code>\n\n"
        f"Каналы будут созданы в фоне. Вы получите уведомление по завершении.\n"
        f"<i>Управление очередью: /ops → 📋 Очередь</i>",
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# 3. BULK EDIT CHANNELS  (FSM: EditChannelBulkFSM)
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanFactCb.filter(F.action == "bulk_edit"))
async def cb_chanf_bulk_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EditChannelBulkFSM.choosing_field)
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Название", callback_data=ChanFactCb(action="be_field_title"))
    kb.button(text="📄 Описание", callback_data=ChanFactCb(action="be_field_about"))
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(2, 1)
    await callback.message.edit_text(
        "✏️ <b>Массовое редактирование каналов</b>\n\nЧто изменить?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    ChanFactCb.filter(F.action.in_({"be_field_title", "be_field_about"}))
)
async def cb_chanf_be_field(
    callback: CallbackQuery, callback_data: ChanFactCb, state: FSMContext
) -> None:
    await callback.answer()
    field = "title" if callback_data.action == "be_field_title" else "about"
    await state.update_data(edit_field=field)
    await state.set_state(EditChannelBulkFSM.choosing_scope)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🌍 Все каналы (все аккаунты)",
        callback_data=ChanFactCb(action="be_scope_all"),
    )
    kb.button(text="👤 По аккаунту", callback_data=ChanFactCb(action="be_scope_acc"))
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="bulk_edit"))
    kb.adjust(1)
    field_label = "названия" if field == "title" else "описания"
    await callback.message.edit_text(
        f"✏️ Изменение <b>{field_label}</b>\n\nВыберите охват:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "be_scope_all"))
async def cb_chanf_be_scope_all(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    await state.update_data(be_scope="all", be_acc_ids=[a["id"] for a in accounts])
    await state.set_state(EditChannelBulkFSM.waiting_value)
    data = await state.get_data()
    field_label = "Название" if data["edit_field"] == "title" else "Описание"
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    await callback.message.edit_text(
        f"✏️ Новое <b>{field_label}</b> (применится ко всем каналам):\n\nВведите текст:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "be_scope_acc"))
async def cb_chanf_be_scope_acc(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=_acc_label(acc),
            callback_data=ChanFactCb(action="be_pick_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="bulk_edit"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👤 Выберите аккаунт для фильтрации:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "be_pick_acc"))
async def cb_chanf_be_pick_acc(
    callback: CallbackQuery, callback_data: ChanFactCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.update_data(be_scope="account", be_acc_ids=[callback_data.acc_id])
    await state.set_state(EditChannelBulkFSM.waiting_value)
    data = await state.get_data()
    field_label = "Название" if data["edit_field"] == "title" else "Описание"
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    await callback.message.edit_text(
        f"✏️ Новое <b>{field_label}</b>:\n\nВведите текст:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(EditChannelBulkFSM.waiting_value)
async def fsm_be_value(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
        await message.answer(
            "⚠️ Введите непустое значение:", reply_markup=kb.as_markup()
        )
        return
    await state.update_data(edit_value=value)
    await state.set_state(EditChannelBulkFSM.previewing)
    data = await state.get_data()
    field = data["edit_field"]
    scope = data["be_scope"]
    field_label = "название" if field == "title" else "описание"
    scope_label = "все каналы" if scope == "all" else "каналы выбранного аккаунта"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Применить", callback_data=ChanFactCb(action="be_confirm"))
    kb.button(text="❌ Отмена", callback_data=ChanFactCb(action="menu"))
    kb.adjust(2)
    await message.answer(
        f"🔍 <b>Предпросмотр изменения</b>\n\n"
        f"Поле: <b>{field_label}</b>\n"
        f"Охват: <b>{scope_label}</b>\n"
        f"Новое значение: <b>{html.escape(value)}</b>\n\n"
        "Применить изменения?",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "be_confirm"))
async def cb_chanf_be_confirm(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Применяю изменения...")
    data = await state.get_data()
    await state.clear()

    field = data.get("edit_field", "title")
    value = data.get("edit_value", "")
    acc_ids: list[int] = data.get("be_acc_ids", [])

    if not acc_ids:
        await callback.message.edit_text(
            "⚠️ Нет аккаунтов для операции.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    accounts = await pool.fetch(
        "SELECT id, session_str, first_name, phone, device_model, system_version, app_version "
        "FROM tg_accounts WHERE owner_id=$1 AND id = ANY($2::bigint[])",
        callback.from_user.id,
        acc_ids,
    )

    from services import account_manager

    ok_total = 0
    err_total = 0
    progress_msg = await callback.message.edit_text(
        "⏳ <b>Загружаю каналы и применяю изменения...</b>",
        parse_mode="HTML",
    )

    for acc in accounts:
        try:
            dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
        except Exception as _e:
            log.warning("bulk_edit get_dialogs failed acc=%s: %s", acc.get("id"), _e)
            err_total += 1
            continue
        channels = [
            d
            for d in dialogs
            if d.get("type") in ("channel", "megagroup", "supergroup")
        ]
        for ch in channels:
            ch_id = ch["id"]
            if field == "title":
                ok = await account_manager.edit_channel_title(
                    acc["session_str"], ch_id, value, _acc=acc
                )
            else:
                ok = await account_manager.edit_channel_about(
                    acc["session_str"], ch_id, value, _acc=acc
                )
            if ok:
                ok_total += 1
            else:
                err_total += 1
            await asyncio.sleep(backoff(1, base=2.0, cap=10.0))

    await progress_msg.edit_text(
        f"✅ <b>Изменение применено</b>\n\n"
        f"✅ Успешно: {ok_total}\n"
        f"❌ Ошибок: {err_total}",
        parse_mode="HTML",
        reply_markup=_back_menu_kb().as_markup(),
    )


# ══════════════════════════════════════════════════════════════════════════
# 4. GENERATE INVITE LINKS
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanFactCb.filter(F.action == "gen_links"))
async def cb_chanf_gen_links(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=_acc_label(acc),
            callback_data=ChanFactCb(action="gen_links_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(2)
    await callback.message.edit_text(
        "🔗 <b>Генерация ссылок</b>\n\nВыберите аккаунт для загрузки каналов:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "gen_links_acc"))
async def cb_chanf_gen_links_acc(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    acc = await pool.fetchrow(
        "SELECT session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю каналы...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
    except Exception as _e:
        log.warning("gen_links get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить список каналов: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    channels = [
        d for d in dialogs if d.get("type") in ("channel", "megagroup", "supergroup")
    ]
    if not channels:
        kb_empty = InlineKeyboardBuilder()
        kb_empty.button(
            text="📥 Импортировать каналы", callback_data=ChanFactCb(action="import")
        )
        kb_empty.button(text="◀️ Назад", callback_data=ChanFactCb(action="gen_links"))
        kb_empty.adjust(1)
        await callback.message.edit_text(
            "ℹ️ <b>Нет каналов для этого аккаунта</b>\n\n"
            "💡 Сначала импортируйте каналы из Telegram в разделе <b>📥 Импорт из Telegram</b>, "
            "затем вы сможете генерировать ссылки.",
            parse_mode="HTML",
            reply_markup=kb_empty.as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for ch in channels[:20]:
        title = (ch.get("title") or f"id={ch['id']}")[:30]
        kb.button(
            text=f"🔗 {title}",
            callback_data=ChanFactCb(
                action="gen_link",
                acc_id=callback_data.acc_id,
                channel_id=ch["id"],
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="gen_links"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🔗 <b>Выберите канал для генерации ссылки:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "gen_link"))
async def cb_chanf_gen_link(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    acc = await pool.fetchrow(
        "SELECT session_str FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Генерирую ссылку...")
    from services import account_manager

    link = await account_manager.get_channel_invite_link(
        acc["session_str"], callback_data.channel_id, _acc=acc
    )
    if link:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад к каналам",
            callback_data=ChanFactCb(
                action="gen_links_acc", acc_id=callback_data.acc_id
            ),
        )
        await callback.message.edit_text(
            f"🔗 <b>Ссылка-приглашение</b>\n\n<code>{html.escape(link)}</code>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await callback.message.edit_text(
            "❌ Не удалось получить ссылку. Проверьте права аккаунта.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )


# ══════════════════════════════════════════════════════════════════════════
# 5. CHANNEL STATS
# ══════════════════════════════════════════════════════════════════════════


@router.callback_query(ChanFactCb.filter(F.action == "stats"))
async def cb_chanf_stats(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    """Step 1: choose account to list channels from."""
    await callback.answer()
    accounts = await _get_active_accounts(pool, callback.from_user.id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\n"
            "Для просмотра статистики каналов нужен хотя бы один активный аккаунт.\n\n"
            "Добавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=_no_accounts_kb().as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=_acc_label(acc),
            callback_data=ChanFactCb(action="stats_acc", acc_id=acc["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    kb.adjust(2)
    await callback.message.edit_text(
        "📊 <b>Статистика каналов</b>\n\nВыберите аккаунт для загрузки списка каналов:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "stats_acc"))
async def cb_chanf_stats_acc(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    """Step 2: load channel list for chosen account."""
    acc = await pool.fetchrow(
        "SELECT id, session_str, first_name, phone, username, "
        "device_model, system_version, app_version "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Загружаю каналы...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
    except Exception as _e:
        log.warning("stats_acc get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить список каналов: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    channels = [
        d for d in dialogs if d.get("type") in ("channel", "megagroup", "supergroup")
    ]

    if not channels:
        kb_empty = InlineKeyboardBuilder()
        kb_empty.button(
            text="📥 Импортировать каналы", callback_data=ChanFactCb(action="import")
        )
        kb_empty.button(text="◀️ Назад", callback_data=ChanFactCb(action="stats"))
        kb_empty.adjust(1)
        await callback.message.edit_text(
            "ℹ️ <b>Нет каналов для этого аккаунта</b>\n\n"
            "💡 Сначала импортируйте каналы из Telegram в разделе <b>📥 Импорт из Telegram</b>, "
            "затем статистика будет доступна.",
            parse_mode="HTML",
            reply_markup=kb_empty.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for ch in channels[:20]:
        title = (ch.get("title") or f"id={ch['id']}")[:30]
        kb.button(
            text=f"📊 {title}",
            callback_data=ChanFactCb(
                action="stats_chan",
                acc_id=callback_data.acc_id,
                channel_id=ch["id"],
            ),
        )
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="stats"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📊 <b>Выберите канал</b>\n\nНайдено каналов: <b>{len(channels)}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "stats_chan"))
async def cb_chanf_stats_chan(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    """Step 3: show basic stats for the chosen channel."""
    acc = await pool.fetchrow(
        "SELECT id, session_str, first_name, phone, username, "
        "device_model, system_version, app_version "
        "FROM tg_accounts WHERE id=$1 AND owner_id=$2",
        callback_data.acc_id,
        callback.from_user.id,
    )
    if not acc:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    await callback.answer("⏳ Получаю статистику...")
    from services import account_manager

    try:
        dialogs = await account_manager.get_dialogs(acc["session_str"], _acc=acc) or []
    except Exception as _e:
        log.warning("stats_channel get_dialogs failed acc=%s: %s", acc.get("id"), _e)
        await callback.message.edit_text(
            f"❌ Не удалось получить данные канала: <code>{html.escape(str(_e)[:150])}</code>",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    chan_data = next((d for d in dialogs if d["id"] == callback_data.channel_id), None)

    if not chan_data:
        await callback.message.edit_text(
            "⚠️ Канал не найден в списке диалогов.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return

    title = html.escape(chan_data.get("title") or f"id={chan_data['id']}")
    username = chan_data.get("username", "")
    ch_id = chan_data["id"]
    ch_type = chan_data.get("type", "channel")
    members = chan_data.get("members", 0) or 0

    # If members count is 0, try fetching via get_channel_members_count
    if members == 0 and username:
        try:
            fetched = await account_manager.get_channel_members_count(
                acc["session_str"], username, _acc=acc
            )
            if fetched > 0:
                members = fetched
        except Exception:
            log_exc_swallow(log, "Не удалось получить количество участников канала")

    type_label = {
        "channel": "📡 Канал",
        "megagroup": "👥 Супергруппа",
        "supergroup": "👥 Супергруппа",
        "group": "👥 Группа",
    }.get(ch_type, "📡 Канал")

    uname_line = f"@{html.escape(username)}" if username else "—"

    text = (
        f"📊 <b>Статистика канала</b>\n\n"
        f"Название: <b>{title}</b>\n"
        f"Тип: {type_label}\n"
        f"ID: <code>{ch_id}</code>\n"
        f"Username: {uname_line}\n"
        f"Участников: <b>{members:,}</b>"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ К списку каналов",
        callback_data=ChanFactCb(action="stats_acc", acc_id=callback_data.acc_id),
    )
    kb.button(text="🏠 Меню", callback_data=ChanFactCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── SEO Pick (account → channel picker → SeoCb) ────────────────────────────

_SEO_PAGE = 8


@router.callback_query(ChanFactCb.filter(F.action == "seo_pick"))
async def cb_chanf_seo_pick_acc(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    user_id = callback.from_user.id
    accounts = await _get_active_accounts(pool, user_id)
    if not accounts:
        await callback.message.edit_text(
            "⚠️ <b>Нет активных аккаунтов</b>\n\nДобавьте аккаунт в разделе 📱 Аккаунты.",
            parse_mode="HTML",
            reply_markup=_back_menu_kb().as_markup(),
        )
        return
    if len(accounts) == 1:
        # Skip account picker if only one account
        await _show_seo_chan_picker(callback, pool, accounts[0]["id"], user_id, page=0)
        return
    kb = InlineKeyboardBuilder()
    for acc in accounts:
        kb.button(
            text=_acc_label(acc),
            callback_data=ChanFactCb(action="seo_acc", acc_id=acc["id"]),
        )
    kb.adjust(1)
    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="menu"))
    await callback.message.edit_text(
        "📈 <b>SEO-оптимизация канала</b>\n\nВыберите аккаунт, которому принадлежит канал:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ChanFactCb.filter(F.action == "seo_acc"))
async def cb_chanf_seo_acc(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await _show_seo_chan_picker(
        callback, pool, callback_data.acc_id, callback.from_user.id, page=0
    )


@router.callback_query(ChanFactCb.filter(F.action == "seo_chan_page"))
async def cb_chanf_seo_chan_page(
    callback: CallbackQuery, callback_data: ChanFactCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await _show_seo_chan_picker(
        callback,
        pool,
        callback_data.acc_id,
        callback.from_user.id,
        page=callback_data.page,
    )


async def _show_seo_chan_picker(
    callback: CallbackQuery, pool: asyncpg.Pool, acc_id: int, user_id: int, page: int
) -> None:
    offset = page * _SEO_PAGE
    channels = await pool.fetch(
        "SELECT id, title, username FROM managed_channels "
        "WHERE owner_id=$1 AND acc_id=$2 ORDER BY title LIMIT $3 OFFSET $4",
        user_id,
        acc_id,
        _SEO_PAGE + 1,
        offset,
    )
    if not channels and page == 0:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="📥 Импорт из Telegram", callback_data=ChanFactCb(action="import")
        )
        kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="seo_pick"))
        kb.adjust(1)
        await callback.message.edit_text(
            "📭 <b>Нет каналов для SEO-анализа</b>\n\n"
            "Импортируйте каналы через меню <b>Каналы → Импорт из Telegram</b>, "
            "после этого SEO-оптимизация станет доступна для каждого канала.\n\n"
            "💡 Импорт сканирует все каналы и группы, где вы являетесь администратором.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    has_more = len(channels) > _SEO_PAGE
    channels = channels[:_SEO_PAGE]

    kb = InlineKeyboardBuilder()
    for ch in channels:
        label = (
            f"@{ch['username']}"
            if ch.get("username")
            else ch["title"] or f"id{ch['id']}"
        )
        kb.button(
            text=f"📡 {label[:35]}",
            callback_data=SeoCb(action="chan_menu", chan_id=ch["id"], acc_id=acc_id),
        )
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(
            text="◀️",
            callback_data=ChanFactCb(
                action="seo_chan_page", acc_id=acc_id, page=page - 1
            ),
        )
    if has_more:
        nav.button(
            text="▶️",
            callback_data=ChanFactCb(
                action="seo_chan_page", acc_id=acc_id, page=page + 1
            ),
        )
    if page > 0 or has_more:
        nav.adjust(2)
        kb.attach(nav)

    kb.button(text="◀️ Назад", callback_data=ChanFactCb(action="seo_pick"))
    await callback.message.edit_text(
        "📈 <b>SEO-оптимизация — выберите канал:</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

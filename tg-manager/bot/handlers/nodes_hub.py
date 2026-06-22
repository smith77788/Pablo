"""BotMother Nodes Hub — управление Telegram Forum Workspaces.

Flows:
  menu        → список воркспейсов
  add         → FSM: chat_id → тип → имя → confirm → register
  view        → статистика воркспейса + кнопки действий
  threads     → список активных топиков
  provision   → FSM: entity_type → entity_id → topic_name → create topic
  broadcast   → FSM: text → confirm → STRIKE отправка всем топикам
  bulk_close  → закрыть все открытые топики воркспейса
  thread_close→ закрыть один топик
  enable_forum→ включить форум-режим через Telethon
  remove      → деактивировать воркспейс

Reverse command routing:
  Входящие сообщения из форум-топиков →
  route_node_command() → определяет entity → dispatch action
"""

from __future__ import annotations

import html
import logging

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, NodesCb
from bot.states import (
    NodesAddFSM,
    NodesBroadcastFSM,
    NodesBulkFSM,
    NodesProvisionFSM,
)
from services import nodes_engine
from services.nodes_engine import ENTITY_LABELS, NODE_TYPE_LABELS

log = logging.getLogger(__name__)
router = Router()

_NODE_TYPES = list(NODE_TYPE_LABELS.keys())
_ENTITY_TYPES = list(ENTITY_LABELS.keys())

# ── Keyboards ─────────────────────────────────────────────────────────────────


def _back_menu_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=NodesCb(action="menu"))
    return kb.as_markup()


def _back_node_kb(node_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=NodesCb(action="view", node_id=node_id))
    return kb.as_markup()


def _cancel_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NodesCb(action="menu"))
    return kb.as_markup()


def _cancel_node_kb(node_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=NodesCb(action="view", node_id=node_id))
    return kb.as_markup()


# ── Menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "menu"))
async def cb_nodes_menu(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    owner = callback.from_user.id
    workspaces = await nodes_engine.get_workspaces(pool, owner)

    kb = InlineKeyboardBuilder()
    for ws in workspaces:
        label = f"{NODE_TYPE_LABELS.get(ws['node_type'], ws['node_type'])} — {html.escape(ws['name'])}"
        kb.button(text=label, callback_data=NodesCb(action="view", node_id=ws["id"]))
    kb.button(text="➕ Добавить воркспейс", callback_data=NodesCb(action="add"))
    kb.button(text="◀️ Главное меню", callback_data=BmCb(action="main"))
    kb.adjust(1)

    count = len(workspaces)
    text = (
        "<b>📡 BotMother Nodes</b>\n\n"
        "Nodes — это форум-группы Telegram, где каждая инфраструктурная сущность "
        "(прокси, аккаунт, воркер) получает собственный топик-тред для логов и команд.\n\n"
        f"Активных воркспейсов: <b>{count}</b>"
    )
    if not workspaces:
        text += "\n\n💡 Нажмите <b>➕ Добавить</b>, чтобы подключить форум-группу."

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Add workspace — FSM ───────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "add"))
async def cb_nodes_add(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(NodesAddFSM.waiting_chat_id)
    await callback.message.edit_text(
        "<b>📡 Новый воркспейс — шаг 1/3</b>\n\n"
        "Отправьте <b>Chat ID</b> форум-суперgroup'ы.\n\n"
        "Как получить ID:\n"
        "• Добавьте @userinfobot в группу\n"
        "• Или используйте <code>getUpdates</code> в Bot API\n\n"
        "Формат: отрицательное число, например <code>-1001234567890</code>",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(StateFilter(NodesAddFSM.waiting_chat_id))
async def msg_nodes_chat_id(message: Message, state: FSMContext) -> None:
    raw = message.text.strip() if message.text else ""
    try:
        chat_id = int(raw)
    except (TypeError, ValueError):
        await message.answer(
            "❌ Неверный формат. Введите числовой Chat ID, например <code>-1001234567890</code>",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        return

    await state.update_data(chat_id=chat_id)
    await state.set_state(NodesAddFSM.waiting_name)

    kb = InlineKeyboardBuilder()
    for ntype, label in NODE_TYPE_LABELS.items():
        kb.button(text=label, callback_data=NodesCb(action=f"add_type_{ntype}"))
    kb.button(text="❌ Отмена", callback_data=NodesCb(action="menu"))
    kb.adjust(1)

    await message.answer(
        f"<b>📡 Новый воркспейс — шаг 2/3</b>\n\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
        "Выберите тип воркспейса:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NodesCb.filter(F.action.startswith("add_type_")))
async def cb_nodes_add_type(
    callback: CallbackQuery,
    callback_data: NodesCb,
    state: FSMContext,
    pool: asyncpg.Pool,
    bot: Bot,
) -> None:
    fsm_data = await state.get_data()
    chat_id = fsm_data.get("chat_id")
    if not chat_id:
        await callback.answer("Сессия истекла. Начните заново.", show_alert=True)
        await state.clear()
        return

    node_type = callback_data.action.removeprefix("add_type_")
    if node_type not in NODE_TYPE_LABELS:
        await callback.answer("Неизвестный тип.", show_alert=True)
        return

    await callback.answer()
    owner = callback.from_user.id
    default_name = NODE_TYPE_LABELS[node_type]

    try:
        node = await nodes_engine.initialize_workspace(
            pool=pool,
            bot=bot,
            owner_id=owner,
            tg_chat_id=chat_id,
            node_type=node_type,
            name=default_name,
        )
    except ValueError as exc:
        await callback.message.edit_text(
            f"❌ Ошибка: {html.escape(str(exc))}",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        await state.clear()
        return
    except Exception as exc:
        log.error("nodes_hub: initialize_workspace failed: %s", exc)
        await callback.message.edit_text(
            "❌ Ошибка регистрации воркспейса. Проверьте, что бот добавлен в группу как администратор.",
            parse_mode="HTML",
            reply_markup=_cancel_kb(),
        )
        await state.clear()
        return

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Открыть воркспейс", callback_data=NodesCb(action="view", node_id=node["id"]))
    kb.button(text="🔧 Включить форум-режим", callback_data=NodesCb(action="enable_forum", node_id=node["id"]))
    kb.button(text="◀️ К списку", callback_data=NodesCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>Воркспейс зарегистрирован!</b>\n\n"
        f"Тип: <b>{html.escape(NODE_TYPE_LABELS[node_type])}</b>\n"
        f"Chat ID: <code>{chat_id}</code>\n"
        f"Node ID: <code>{node['id']}</code>\n\n"
        "⚠️ Убедитесь, что группа является форум-суперgroup'ой.\n"
        "Если ещё нет — нажмите <b>«Включить форум-режим»</b>.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Enable forum mode ─────────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "enable_forum"))
async def cb_nodes_enable_forum(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("Включаю форум-режим…")
    owner = callback.from_user.id
    node = await nodes_engine.get_node_by_id(pool, callback_data.node_id, owner)
    if not node:
        await callback.answer("Воркспейс не найден.", show_alert=True)
        return

    ok = await nodes_engine.enable_forum_mode(pool, owner, node["tg_chat_id"])

    if ok:
        await callback.message.edit_text(
            "✅ <b>Форум-режим включён!</b>\n\n"
            f"Группа <code>{node['tg_chat_id']}</code> теперь является форум-суперgroup'ой.\n"
            "Можете создавать топики для инфраструктурных сущностей.",
            parse_mode="HTML",
            reply_markup=_back_node_kb(callback_data.node_id),
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Не удалось включить форум-режим.</b>\n\n"
            "Возможные причины:\n"
            "• Нет доступных аккаунтов Telegram\n"
            "• Аккаунт не является администратором группы\n"
            "• Группа не является суперgroup'ой\n\n"
            "Включите форум-режим вручную: Группа → Изменить → Темы → Включить",
            parse_mode="HTML",
            reply_markup=_back_node_kb(callback_data.node_id),
        )


# ── View workspace ────────────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "view"))
async def cb_nodes_view(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner = callback.from_user.id
    node = await nodes_engine.get_node_by_id(pool, callback_data.node_id, owner)
    if not node:
        await callback.message.edit_text(
            "❌ Воркспейс не найден.", reply_markup=_back_menu_kb()
        )
        return

    stats = await nodes_engine.get_thread_stats(pool, node["id"])

    kb = InlineKeyboardBuilder()
    kb.button(
        text=f"🗂 Топики ({stats['open_count']} активных)",
        callback_data=NodesCb(action="threads", node_id=node["id"]),
    )
    kb.button(
        text="➕ Создать топик",
        callback_data=NodesCb(action="provision", node_id=node["id"]),
    )
    kb.button(
        text="⚡ STRIKE: Массовое создание",
        callback_data=NodesCb(action="bulk_create", node_id=node["id"]),
    )
    kb.button(
        text="📢 Broadcast в все топики",
        callback_data=NodesCb(action="broadcast", node_id=node["id"]),
    )
    kb.button(
        text="🔒 Закрыть все топики",
        callback_data=NodesCb(action="bulk_close", node_id=node["id"]),
    )
    kb.button(
        text="🔧 Включить форум-режим",
        callback_data=NodesCb(action="enable_forum", node_id=node["id"]),
    )
    kb.button(
        text="🗑 Деактивировать воркспейс",
        callback_data=NodesCb(action="remove", node_id=node["id"]),
    )
    kb.button(text="◀️ Назад", callback_data=NodesCb(action="menu"))
    kb.adjust(1)

    node_type_label = NODE_TYPE_LABELS.get(node["node_type"], node["node_type"])
    await callback.message.edit_text(
        f"<b>📡 {html.escape(node['name'])}</b>\n\n"
        f"Тип: {html.escape(node_type_label)}\n"
        f"Chat ID: <code>{node['tg_chat_id']}</code>\n"
        f"Node ID: <code>{node['id']}</code>\n\n"
        f"<b>Статистика топиков:</b>\n"
        f"  🟢 Открытых: <b>{stats['open_count']}</b>\n"
        f"  🔒 Архивных: <b>{stats['archived_count']}</b>\n"
        f"  📊 Всего: <b>{stats['total_count']}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── List threads ──────────────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "threads"))
async def cb_nodes_threads(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner = callback.from_user.id
    node = await nodes_engine.get_node_by_id(pool, callback_data.node_id, owner)
    if not node:
        await callback.message.edit_text("❌ Воркспейс не найден.", reply_markup=_back_menu_kb())
        return

    threads = await nodes_engine.get_threads(pool, node["id"], status="open", limit=30)

    kb = InlineKeyboardBuilder()
    for t in threads:
        entity_label = ENTITY_LABELS.get(t["entity_type"], t["entity_type"])
        btn_label = f"{entity_label} #{t['entity_id']} — {t['topic_name'][:20]}"
        kb.button(
            text=btn_label,
            callback_data=NodesCb(
                action="thread_view",
                node_id=node["id"],
                thread_id=t["id"],
                entity_type=t["entity_type"],
            ),
        )
    kb.button(text="◀️ Назад", callback_data=NodesCb(action="view", node_id=node["id"]))
    kb.adjust(1)

    if threads:
        text = (
            f"<b>🗂 Топики — {html.escape(node['name'])}</b>\n\n"
            f"Открытых топиков: <b>{len(threads)}</b>\n"
            "Нажмите на топик для управления:"
        )
    else:
        text = (
            f"<b>🗂 Топики — {html.escape(node['name'])}</b>\n\n"
            "Нет открытых топиков.\n"
            "Используйте <b>«➕ Создать топик»</b> для подключения сущностей."
        )

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(NodesCb.filter(F.action == "thread_view"))
async def cb_thread_view(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    thread = await pool.fetchrow(
        "SELECT * FROM bm_node_threads WHERE id=$1 AND node_id=$2",
        callback_data.thread_id, callback_data.node_id,
    )
    if not thread:
        await callback.message.edit_text("❌ Топик не найден.", reply_markup=_back_menu_kb())
        return

    entity_label = ENTITY_LABELS.get(thread["entity_type"], thread["entity_type"])

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔒 Закрыть топик",
        callback_data=NodesCb(
            action="thread_close",
            node_id=callback_data.node_id,
            thread_id=thread["id"],
            entity_type=thread["entity_type"],
        ),
    )
    kb.button(text="◀️ Назад", callback_data=NodesCb(action="threads", node_id=callback_data.node_id))
    kb.adjust(1)

    await callback.message.edit_text(
        f"<b>📌 {html.escape(thread['topic_name'])}</b>\n\n"
        f"Тип: {html.escape(entity_label)}\n"
        f"Entity ID: <code>{thread['entity_id']}</code>\n"
        f"Thread ID: <code>{thread['tg_thread_id']}</code>\n"
        f"Статус: <b>{'🟢 Открыт' if thread['status'] == 'open' else '🔒 Архив'}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Provision thread — FSM ────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "provision"))
async def cb_nodes_provision(
    callback: CallbackQuery,
    callback_data: NodesCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner = callback.from_user.id
    node = await nodes_engine.get_node_by_id(pool, callback_data.node_id, owner)
    if not node:
        await callback.answer("Воркспейс не найден.", show_alert=True)
        return

    await state.set_state(NodesProvisionFSM.waiting_entity_type)
    await state.update_data(node_id=node["id"], node_chat_id=node["tg_chat_id"])

    kb = InlineKeyboardBuilder()
    for etype, label in ENTITY_LABELS.items():
        kb.button(
            text=label,
            callback_data=NodesCb(action=f"prov_type_{etype}", node_id=node["id"]),
        )
    kb.button(text="❌ Отмена", callback_data=NodesCb(action="view", node_id=node["id"]))
    kb.adjust(1)

    await callback.message.edit_text(
        "<b>➕ Создать топик — шаг 1/3</b>\n\nВыберите тип сущности:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NodesCb.filter(F.action.startswith("prov_type_")))
async def cb_prov_type(
    callback: CallbackQuery,
    callback_data: NodesCb,
    state: FSMContext,
) -> None:
    entity_type = callback_data.action.removeprefix("prov_type_")
    if entity_type not in ENTITY_LABELS:
        await callback.answer("Неизвестный тип.", show_alert=True)
        return

    fsm_data = await state.get_data()
    if not fsm_data.get("node_id"):
        await callback.answer("Сессия истекла.", show_alert=True)
        await state.clear()
        return

    await callback.answer()
    await state.update_data(entity_type=entity_type)
    await state.set_state(NodesProvisionFSM.waiting_entity_id)

    await callback.message.edit_text(
        f"<b>➕ Создать топик — шаг 2/3</b>\n\n"
        f"Тип: {html.escape(ENTITY_LABELS[entity_type])}\n\n"
        "Введите <b>ID сущности</b> (числовой ID прокси/аккаунта/воркера):",
        parse_mode="HTML",
        reply_markup=_cancel_node_kb(fsm_data["node_id"]),
    )


@router.message(StateFilter(NodesProvisionFSM.waiting_entity_id))
async def msg_prov_entity_id(message: Message, state: FSMContext) -> None:
    raw = message.text.strip() if message.text else ""
    try:
        entity_id = int(raw)
    except (TypeError, ValueError):
        fsm_data = await state.get_data()
        await message.answer(
            "❌ Введите числовой ID.",
            reply_markup=_cancel_node_kb(fsm_data.get("node_id", 0)),
        )
        return

    await state.update_data(entity_id=entity_id)
    await state.set_state(NodesProvisionFSM.waiting_topic_name)
    fsm_data = await state.get_data()

    await message.answer(
        f"<b>➕ Создать топик — шаг 3/3</b>\n\n"
        f"ID сущности: <code>{entity_id}</code>\n\n"
        "Введите <b>название топика</b> (до 128 символов):",
        parse_mode="HTML",
        reply_markup=_cancel_node_kb(fsm_data.get("node_id", 0)),
    )


@router.message(StateFilter(NodesProvisionFSM.waiting_topic_name))
async def msg_prov_topic_name(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    bot: Bot,
) -> None:
    topic_name = (message.text or "").strip()[:128]
    if not topic_name:
        await message.answer("❌ Название не может быть пустым.")
        return

    fsm_data = await state.get_data()
    node_id = fsm_data.get("node_id")
    entity_type = fsm_data.get("entity_type", "")
    entity_id = fsm_data.get("entity_id")

    if not all([node_id, entity_type, entity_id]):
        await message.answer("❌ Сессия истекла. Начните заново.", reply_markup=_back_menu_kb())
        await state.clear()
        return

    await state.clear()
    owner = message.from_user.id

    # Fetch node to get tg_chat_id
    node = await nodes_engine.get_node_by_id(pool, node_id, owner)
    if not node:
        await message.answer("❌ Воркспейс не найден.", reply_markup=_back_menu_kb())
        return

    thread = await nodes_engine.provision_thread_for_entity(
        pool=pool,
        bot=bot,
        owner_id=owner,
        entity_type=entity_type,
        entity_id=entity_id,
        topic_name=topic_name,
        tg_chat_id=node["tg_chat_id"],
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К воркспейсу", callback_data=NodesCb(action="view", node_id=node_id))
    kb.adjust(1)

    if thread:
        entity_label = ENTITY_LABELS.get(entity_type, entity_type)
        await message.answer(
            f"✅ <b>Топик создан!</b>\n\n"
            f"Сущность: {html.escape(entity_label)} #{entity_id}\n"
            f"Топик: <b>{html.escape(topic_name)}</b>\n"
            f"Thread ID: <code>{thread['tg_thread_id']}</code>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    else:
        await message.answer(
            "❌ <b>Не удалось создать топик.</b>\n\n"
            "Проверьте:\n"
            "• Бот является администратором форум-группы\n"
            "• Группа включена в форум-режим\n"
            "• Chat ID корректен",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


# ── STRIKE: bulk create ───────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "bulk_create"))
async def cb_nodes_bulk_create(
    callback: CallbackQuery,
    callback_data: NodesCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner = callback.from_user.id
    node = await nodes_engine.get_node_by_id(pool, callback_data.node_id, owner)
    if not node:
        await callback.answer("Воркспейс не найден.", show_alert=True)
        return

    await state.set_state(NodesBulkFSM.waiting_entity_type)
    await state.update_data(node_id=node["id"])

    kb = InlineKeyboardBuilder()
    for etype, label in ENTITY_LABELS.items():
        kb.button(
            text=label,
            callback_data=NodesCb(action=f"bulk_etype_{etype}", node_id=node["id"]),
        )
    kb.button(text="❌ Отмена", callback_data=NodesCb(action="view", node_id=node["id"]))
    kb.adjust(1)

    await callback.message.edit_text(
        "<b>⚡ STRIKE: Массовое создание топиков</b>\n\n"
        "Шаг 1/2 — Выберите тип сущностей:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NodesCb.filter(F.action.startswith("bulk_etype_")))
async def cb_bulk_etype(
    callback: CallbackQuery,
    callback_data: NodesCb,
    state: FSMContext,
) -> None:
    entity_type = callback_data.action.removeprefix("bulk_etype_")
    if entity_type not in ENTITY_LABELS:
        await callback.answer("Неизвестный тип.", show_alert=True)
        return

    fsm_data = await state.get_data()
    if not fsm_data.get("node_id"):
        await callback.answer("Сессия истекла.", show_alert=True)
        await state.clear()
        return

    await callback.answer()
    await state.update_data(entity_type=entity_type)
    await state.set_state(NodesBulkFSM.waiting_ids)

    await callback.message.edit_text(
        f"<b>⚡ STRIKE: Массовое создание — шаг 2/2</b>\n\n"
        f"Тип: {html.escape(ENTITY_LABELS[entity_type])}\n\n"
        "Введите <b>список ID</b> через запятую:\n"
        "<code>101, 102, 103, 104, 105</code>\n\n"
        "⚠️ Скорость: ~2.5 топика/сек (лимит Telegram API).",
        parse_mode="HTML",
        reply_markup=_cancel_node_kb(fsm_data["node_id"]),
    )


@router.message(StateFilter(NodesBulkFSM.waiting_ids))
async def msg_bulk_ids(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    bot: Bot,
) -> None:
    raw = message.text or ""
    try:
        ids = [int(x.strip()) for x in raw.replace("\n", ",").split(",") if x.strip()]
    except (TypeError, ValueError):
        await message.answer("❌ Неверный формат. Пример: <code>101, 102, 103</code>", parse_mode="HTML")
        return

    if not ids:
        await message.answer("❌ Список пустой.")
        return

    if len(ids) > 500:
        await message.answer("❌ Максимум 500 сущностей за один запуск.")
        return

    fsm_data = await state.get_data()
    node_id = fsm_data.get("node_id")
    entity_type = fsm_data.get("entity_type", "")
    await state.clear()

    if not node_id or not entity_type:
        await message.answer("❌ Сессия истекла.", reply_markup=_back_menu_kb())
        return

    owner = message.from_user.id
    node = await nodes_engine.get_node_by_id(pool, node_id, owner)
    if not node:
        await message.answer("❌ Воркспейс не найден.", reply_markup=_back_menu_kb())
        return

    entity_label = ENTITY_LABELS.get(entity_type, entity_type)
    await message.answer(
        f"⚡ <b>STRIKE запущен</b> — создаю {len(ids)} топиков для {html.escape(entity_label)}…\n"
        "Это займёт несколько секунд.",
        parse_mode="HTML",
    )

    entities = [{"id": eid, "name": f"{entity_label} #{eid}"} for eid in ids]
    created, errors = await nodes_engine.strike_bulk_create_threads(
        pool=pool,
        bot=bot,
        owner_id=owner,
        entity_type=entity_type,
        entities=entities,
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К воркспейсу", callback_data=NodesCb(action="view", node_id=node_id))
    kb.adjust(1)

    await message.answer(
        f"✅ <b>STRIKE завершён</b>\n\n"
        f"Создано топиков: <b>{len(created)}</b>\n"
        f"Ошибок: <b>{errors}</b>\n"
        f"Всего запрошено: <b>{len(ids)}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Broadcast — FSM ───────────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "broadcast"))
async def cb_nodes_broadcast(
    callback: CallbackQuery,
    callback_data: NodesCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner = callback.from_user.id
    node = await nodes_engine.get_node_by_id(pool, callback_data.node_id, owner)
    if not node:
        await callback.answer("Воркспейс не найден.", show_alert=True)
        return

    stats = await nodes_engine.get_thread_stats(pool, node["id"])
    open_count = stats["open_count"]

    if open_count == 0:
        await callback.answer("Нет открытых топиков для рассылки.", show_alert=True)
        return

    await state.set_state(NodesBroadcastFSM.waiting_message)
    await state.update_data(node_id=node["id"], node_name=node["name"])

    await callback.message.edit_text(
        f"<b>📢 Broadcast по топикам</b>\n\n"
        f"Воркспейс: <b>{html.escape(node['name'])}</b>\n"
        f"Получателей: <b>{open_count}</b> открытых топиков\n\n"
        "Введите текст алерта (HTML):",
        parse_mode="HTML",
        reply_markup=_cancel_node_kb(node["id"]),
    )


@router.message(StateFilter(NodesBroadcastFSM.waiting_message))
async def msg_broadcast_text(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    bot: Bot,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Текст не может быть пустым.")
        return

    fsm_data = await state.get_data()
    node_id = fsm_data.get("node_id")
    await state.clear()

    if not node_id:
        await message.answer("❌ Сессия истекла.", reply_markup=_back_menu_kb())
        return

    owner = message.from_user.id
    node = await nodes_engine.get_node_by_id(pool, node_id, owner)
    if not node:
        await message.answer("❌ Воркспейс не найден.", reply_markup=_back_menu_kb())
        return

    threads = await nodes_engine.get_threads(pool, node_id, status="open", limit=1000)
    entity_ids = [t["entity_id"] for t in threads]

    # Determine entity_type from threads (mixed or single)
    entity_types = list({t["entity_type"] for t in threads})
    if not entity_types:
        await message.answer("❌ Нет открытых топиков.")
        return

    await message.answer(
        f"📢 Отправляю алерт в <b>{len(threads)}</b> топиков…",
        parse_mode="HTML",
    )

    # Broadcast per entity_type group
    total_sent = 0
    total_failed = 0
    for etype in entity_types:
        etype_ids = [t["entity_id"] for t in threads if t["entity_type"] == etype]
        result = await nodes_engine.strike_broadcast_to_threads(
            pool=pool,
            bot=bot,
            owner_id=owner,
            entity_type=etype,
            entity_ids=etype_ids,
            message=text,
        )
        total_sent += result["sent"]
        total_failed += result["failed"]

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К воркспейсу", callback_data=NodesCb(action="view", node_id=node_id))
    kb.adjust(1)

    await message.answer(
        f"✅ <b>Broadcast завершён</b>\n\n"
        f"Доставлено: <b>{total_sent}</b>\n"
        f"Ошибок: <b>{total_failed}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Bulk close all threads ────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "bulk_close"))
async def cb_nodes_bulk_close(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
    bot: Bot,
) -> None:
    await callback.answer("Закрываю топики…")
    owner = callback.from_user.id
    node = await nodes_engine.get_node_by_id(pool, callback_data.node_id, owner)
    if not node:
        await callback.answer("Воркспейс не найден.", show_alert=True)
        return

    threads = await nodes_engine.get_threads(pool, node["id"], status="open", limit=500)
    if not threads:
        await callback.answer("Нет открытых топиков.", show_alert=True)
        return

    closed = 0
    for t in threads:
        ok = await nodes_engine.close_entity_thread(
            pool=pool,
            bot=bot,
            entity_type=t["entity_type"],
            entity_id=t["entity_id"],
            owner_id=owner,
        )
        if ok:
            closed += 1

    kb = _back_node_kb(callback_data.node_id)
    await callback.message.edit_text(
        f"🔒 <b>Массовое закрытие завершено</b>\n\n"
        f"Закрыто топиков: <b>{closed}</b> из <b>{len(threads)}</b>",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ── Close single thread ───────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "thread_close"))
async def cb_thread_close(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
    bot: Bot,
) -> None:
    await callback.answer()
    owner = callback.from_user.id

    thread = await pool.fetchrow(
        "SELECT t.*, n.tg_chat_id FROM bm_node_threads t "
        "JOIN bm_telegram_nodes n ON n.id = t.node_id "
        "WHERE t.id=$1 AND n.owner_id=$2",
        callback_data.thread_id, owner,
    )
    if not thread:
        await callback.answer("Топик не найден.", show_alert=True)
        return

    ok = await nodes_engine.close_entity_thread(
        pool=pool,
        bot=bot,
        entity_type=thread["entity_type"],
        entity_id=thread["entity_id"],
        owner_id=owner,
    )

    if ok:
        await callback.message.edit_text(
            f"🔒 <b>Топик закрыт</b>\n\n"
            f"{html.escape(thread['topic_name'])} → архив.",
            parse_mode="HTML",
            reply_markup=_back_node_kb(callback_data.node_id),
        )
    else:
        await callback.answer("Не удалось закрыть топик.", show_alert=True)


# ── Remove workspace ──────────────────────────────────────────────────────────


@router.callback_query(NodesCb.filter(F.action == "remove"))
async def cb_nodes_remove(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="⚠️ Да, деактивировать",
        callback_data=NodesCb(action="remove_confirm", node_id=callback_data.node_id),
    )
    kb.button(text="❌ Отмена", callback_data=NodesCb(action="view", node_id=callback_data.node_id))
    kb.adjust(1)

    await callback.message.edit_text(
        "⚠️ <b>Деактивировать воркспейс?</b>\n\n"
        "Записи топиков сохранятся в БД, но воркспейс будет исключён из списка активных.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(NodesCb.filter(F.action == "remove_confirm"))
async def cb_nodes_remove_confirm(
    callback: CallbackQuery,
    callback_data: NodesCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner = callback.from_user.id
    ok = await nodes_engine.deactivate_workspace(pool, callback_data.node_id, owner)

    if ok:
        await callback.message.edit_text(
            "✅ Воркспейс деактивирован.",
            reply_markup=_back_menu_kb(),
        )
    else:
        await callback.message.edit_text(
            "❌ Воркспейс не найден или уже деактивирован.",
            reply_markup=_back_menu_kb(),
        )


# ── Reverse command routing — inbound forum messages ─────────────────────────


@router.message(
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id.is_not(None),
)
async def handle_node_thread_message(
    message: Message,
    pool: asyncpg.Pool,
    bot: Bot,
) -> None:
    """Intercept messages in Node forum threads and route inline commands."""
    if not message.text or not message.text.startswith("/"):
        return  # Only route slash-commands

    tg_chat_id = message.chat.id
    thread_id = message.message_thread_id
    from_uid = message.from_user.id if message.from_user else 0

    ctx = await nodes_engine.route_node_command(
        pool=pool,
        tg_chat_id=tg_chat_id,
        message_thread_id=thread_id,
        text=message.text,
        from_user_id=from_uid,
    )
    if not ctx:
        return  # Not a registered Node thread

    command = ctx.get("command")
    entity_type = ctx["entity_type"]
    entity_id = ctx["entity_id"]
    owner_id = ctx["owner_id"]
    args = ctx.get("args", "")

    log.info(
        "nodes_hub: command /%s in thread entity=%s/%d from user=%d",
        command, entity_type, entity_id, from_uid,
    )

    # Built-in command dispatch
    if command == "status":
        report = nodes_engine.build_status_report(
            entity_type=entity_type,
            entity_id=entity_id,
            status="active",
            details={"owner_id": owner_id, "command": f"/{command}"},
        )
        await message.reply(report, parse_mode="HTML")

    elif command == "close":
        ok = await nodes_engine.close_entity_thread(
            pool=pool,
            bot=bot,
            entity_type=entity_type,
            entity_id=entity_id,
            owner_id=owner_id,
        )
        if ok:
            await message.reply(
                f"🔒 Топик <b>{entity_type} #{entity_id}</b> закрыт.",
                parse_mode="HTML",
            )

    elif command == "info":
        entity_label = ENTITY_LABELS.get(entity_type, entity_type)
        await message.reply(
            f"<b>ℹ️ Node Thread Info</b>\n\n"
            f"Сущность: {html.escape(entity_label)} #{entity_id}\n"
            f"Thread ID: <code>{thread_id}</code>\n"
            f"Chat ID: <code>{tg_chat_id}</code>\n"
            f"Owner: <code>{owner_id}</code>",
            parse_mode="HTML",
        )

    elif command == "help":
        await message.reply(
            "<b>📋 Node Thread Commands</b>\n\n"
            "/status — статус сущности\n"
            "/info — информация о треде\n"
            "/close — архивировать топик\n"
            "/help — список команд",
            parse_mode="HTML",
        )

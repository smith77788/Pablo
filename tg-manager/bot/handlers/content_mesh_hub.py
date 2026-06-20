"""Content Mesh UI — automated content distribution network manager."""

import html
import logging
from datetime import timezone

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ContentMeshCb, BmCb
from bot.states import ContentMeshFSM

log = logging.getLogger(__name__)
router = Router()


# ── helpers ───────────────────────────────────────────────────────────────────


def _back_to_menu():
    return InlineKeyboardBuilder().button(
        text="◀️ К Content Mesh", callback_data=ContentMeshCb(action="menu")
    ).as_markup()


async def _get_mesh(pool: asyncpg.Pool, mesh_id: int, owner_id: int):
    return await pool.fetchrow(
        "SELECT * FROM content_meshes WHERE id=$1 AND owner_id=$2",
        mesh_id, owner_id,
    )


async def _get_account_name(pool: asyncpg.Pool, account_id: int | None) -> str:
    if not account_id:
        return "не задан"
    row = await pool.fetchrow(
        "SELECT phone, username, first_name FROM tg_accounts WHERE id=$1",
        account_id,
    )
    if not row:
        return f"id{account_id}"
    return html.escape(row["username"] or row["first_name"] or row["phone"] or f"id{account_id}")


# ── menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "menu"))
async def cb_mesh_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    meshes = await pool.fetch(
        "SELECT * FROM content_meshes WHERE owner_id=$1 ORDER BY id",
        callback.from_user.id,
    )
    kb = InlineKeyboardBuilder()
    for m in meshes:
        status = "🟢" if m["enabled"] else "🔴"
        src = m["source_channel"] or "—"
        kb.button(
            text=f"{status} {html.escape(m['name'])} ← {html.escape(src)}",
            callback_data=ContentMeshCb(action="view", mesh_id=m["id"]),
        )
    kb.button(text="➕ Создать Mesh", callback_data=ContentMeshCb(action="create"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="comms"))
    kb.adjust(1)

    active = sum(1 for m in meshes if m["enabled"])
    pending = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM mesh_queue WHERE status='pending' AND mesh_id IN (SELECT id FROM content_meshes WHERE owner_id=$1)",
        callback.from_user.id,
    )
    pend_cnt = pending["cnt"] if pending else 0

    await callback.message.edit_text(
        "🕸️ <b>Content Mesh</b>\n\n"
        "Автоматическое копирование постов из источника во все подключённые каналы.\n"
        "Посты отправляются с задержкой и без пометки «переслано».\n\n"
        f"Сетей: <b>{len(meshes)}</b>  |  Активных: <b>{active}</b>  |  В очереди: <b>{pend_cnt}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── create ────────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "create"))
async def cb_mesh_create(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(ContentMeshFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ContentMeshCb(action="menu"))
    await callback.message.edit_text(
        "🕸️ <b>Новая Content Mesh</b>\n\n"
        "Введите название для этой сетки (например: <code>Канал RU → 5 целей</code>).",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ContentMeshFSM.waiting_name, F.text)
async def msg_mesh_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("⚠️ Название должно быть от 1 до 100 символов.", parse_mode="HTML")
        return
    await state.clear()
    mesh_id = await pool.fetchval(
        "INSERT INTO content_meshes (owner_id, name) VALUES ($1, $2) RETURNING id",
        message.from_user.id, name,
    )
    mesh = await pool.fetchrow("SELECT * FROM content_meshes WHERE id=$1", mesh_id)
    await _show_mesh(message, pool, mesh, message.from_user.id, edit=False)


# ── view ──────────────────────────────────────────────────────────────────────


async def _show_mesh(msg_or_cb, pool: asyncpg.Pool, mesh, owner_id: int, edit: bool = True) -> None:
    mesh_id = mesh["id"]
    targets = await pool.fetch("SELECT * FROM mesh_targets WHERE mesh_id=$1 ORDER BY id", mesh_id)
    acc_name = await _get_account_name(pool, mesh["source_account_id"])

    stats = await pool.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE status='pending') AS pend,
               COUNT(*) FILTER (WHERE status='sent') AS sent,
               COUNT(*) FILTER (WHERE status='error') AS err
        FROM mesh_queue WHERE mesh_id=$1
        """,
        mesh_id,
    )
    pend = stats["pend"] if stats else 0
    sent = stats["sent"] if stats else 0
    err  = stats["err"] if stats else 0

    status = "🟢 Активна" if mesh["enabled"] else "🔴 Выключена"
    src = html.escape(mesh["source_channel"] or "не настроен")
    delay = mesh["delay_minutes"]
    cta = html.escape(mesh["append_text"] or "—")

    tgt_lines = "\n".join(
        f"  {'🟢' if t['enabled'] else '🔴'} {html.escape(t['target_channel'])}"
        for t in targets
    ) or "  (нет целей)"

    text = (
        f"🕸️ <b>{html.escape(mesh['name'])}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Источник: <code>{src}</code>\n"
        f"Аккаунт: <b>{acc_name}</b>\n"
        f"Задержка: <b>{delay} мин</b>\n"
        f"Суффикс: {cta}\n\n"
        f"📌 Цели ({len(targets)}):\n{tgt_lines}\n\n"
        f"📊 Очередь: ⏳{pend}  ✅{sent}  ❌{err}"
    )

    kb = InlineKeyboardBuilder()
    toggle = "🔴 Выключить" if mesh["enabled"] else "🟢 Включить"
    kb.button(text=toggle,               callback_data=ContentMeshCb(action="toggle", mesh_id=mesh_id))
    kb.button(text="📡 Источник",         callback_data=ContentMeshCb(action="set_source", mesh_id=mesh_id))
    kb.button(text="📌 Управлять целями", callback_data=ContentMeshCb(action="targets", mesh_id=mesh_id))
    kb.button(text="⚙️ Настройки",        callback_data=ContentMeshCb(action="settings", mesh_id=mesh_id))
    kb.button(text="📋 Логи",             callback_data=ContentMeshCb(action="logs", mesh_id=mesh_id))
    kb.button(text="🗑 Удалить",          callback_data=ContentMeshCb(action="del", mesh_id=mesh_id))
    kb.button(text="◀️ К списку",         callback_data=ContentMeshCb(action="menu"))
    kb.adjust(2, 2, 2, 1)

    if edit:
        await msg_or_cb.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ContentMeshCb.filter(F.action == "view"))
async def cb_mesh_view(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.answer("Mesh не найдена.", show_alert=True)
        return
    await _show_mesh(callback.message, pool, mesh, callback.from_user.id)


# ── toggle ────────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "toggle"))
async def cb_mesh_toggle(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.answer("Mesh не найдена.", show_alert=True)
        return
    new_state = not mesh["enabled"]
    await pool.execute(
        "UPDATE content_meshes SET enabled=$1, updated_at=NOW() WHERE id=$2",
        new_state, callback_data.mesh_id,
    )
    await callback.answer("🟢 Включена" if new_state else "🔴 Выключена")
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    await _show_mesh(callback.message, pool, mesh, callback.from_user.id)


# ── set source ────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "set_source"))
async def cb_mesh_set_source(
    callback: CallbackQuery, callback_data: ContentMeshCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.answer("Mesh не найдена.", show_alert=True)
        return
    await state.set_state(ContentMeshFSM.waiting_source_channel)
    await state.update_data(mesh_id=callback_data.mesh_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ContentMeshCb(action="view", mesh_id=callback_data.mesh_id))
    cur = html.escape(mesh["source_channel"] or "—")
    await callback.message.edit_text(
        f"📡 <b>Источник контента</b>\n\n"
        f"Текущий: <code>{cur}</code>\n\n"
        "Введите <b>@username</b> или числовой ID (например <code>-1001234567890</code>) "
        "канала-источника, откуда будут копироваться посты.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ContentMeshFSM.waiting_source_channel, F.text)
async def msg_mesh_source_channel(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    mesh_id = data["mesh_id"]
    channel = (message.text or "").strip()
    if not channel:
        await message.answer("⚠️ Введите @username или ID канала.", parse_mode="HTML")
        return
    await state.set_state(ContentMeshFSM.waiting_source_account)
    await state.update_data(source_channel=channel)
    # Show available accounts
    accounts = await pool.fetch(
        "SELECT id, phone, username, first_name FROM tg_accounts WHERE owner_id=$1 AND banned=FALSE ORDER BY id LIMIT 20",
        message.from_user.id,
    )
    if not accounts:
        await state.clear()
        await message.answer(
            "⚠️ Нет доступных аккаунтов для чтения источника. Сначала добавьте аккаунт.",
            parse_mode="HTML",
            reply_markup=_back_to_menu(),
        )
        return
    kb = InlineKeyboardBuilder()
    for a in accounts:
        name = html.escape(a["username"] or a["first_name"] or a["phone"] or f"id{a['id']}")
        kb.button(
            text=f"📱 {name}",
            callback_data=ContentMeshCb(action="pick_account", mesh_id=mesh_id, extra=str(a["id"])),
        )
    kb.button(text="❌ Отмена", callback_data=ContentMeshCb(action="view", mesh_id=mesh_id))
    kb.adjust(1)
    await state.clear()
    await pool.execute(
        "UPDATE content_meshes SET source_channel=$1, updated_at=NOW() WHERE id=$2",
        channel, mesh_id,
    )
    await message.answer(
        f"✅ Источник: <code>{html.escape(channel)}</code>\n\n"
        "Выберите аккаунт для чтения этого канала:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ContentMeshCb.filter(F.action == "pick_account"))
async def cb_mesh_pick_account(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    try:
        acc_id = int(callback_data.extra)
    except (ValueError, TypeError):
        await callback.answer("Ошибка ID аккаунта.", show_alert=True)
        return
    await pool.execute(
        "UPDATE content_meshes SET source_account_id=$1, last_post_id=0, updated_at=NOW() WHERE id=$2 AND owner_id=$3",
        acc_id, callback_data.mesh_id, callback.from_user.id,
    )
    await callback.answer("✅ Аккаунт выбран")
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if mesh:
        await _show_mesh(callback.message, pool, mesh, callback.from_user.id)


# ── targets ───────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "targets"))
async def cb_mesh_targets(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.answer("Mesh не найдена.", show_alert=True)
        return
    targets = await pool.fetch(
        "SELECT * FROM mesh_targets WHERE mesh_id=$1 ORDER BY id", callback_data.mesh_id
    )
    kb = InlineKeyboardBuilder()
    for t in targets:
        status = "🟢" if t["enabled"] else "🔴"
        kb.button(
            text=f"{status} {html.escape(t['target_channel'])} ✕",
            callback_data=ContentMeshCb(action="del_target", mesh_id=callback_data.mesh_id, extra=str(t["id"])),
        )
    kb.button(text="➕ Добавить цель", callback_data=ContentMeshCb(action="add_target", mesh_id=callback_data.mesh_id))
    kb.button(text="◀️ Назад", callback_data=ContentMeshCb(action="view", mesh_id=callback_data.mesh_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📌 <b>Цели: {html.escape(mesh['name'])}</b>\n\n"
        "Нажмите на цель чтобы удалить её.\n"
        "Добавьте каналы, куда нужно копировать посты.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ContentMeshCb.filter(F.action == "add_target"))
async def cb_mesh_add_target(
    callback: CallbackQuery, callback_data: ContentMeshCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(ContentMeshFSM.waiting_target_channel)
    await state.update_data(mesh_id=callback_data.mesh_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ContentMeshCb(action="targets", mesh_id=callback_data.mesh_id))
    await callback.message.edit_text(
        "➕ <b>Добавить цель</b>\n\n"
        "Введите <b>@username</b> или числовой ID канала-получателя.\n"
        "Аккаунт-источника должен быть администратором этого канала.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ContentMeshFSM.waiting_target_channel, F.text)
async def msg_mesh_target_channel(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    mesh_id = data["mesh_id"]
    await state.clear()
    channel = (message.text or "").strip()
    if not channel:
        await message.answer("⚠️ Введите @username или ID канала.", parse_mode="HTML")
        return
    existing = await pool.fetchrow(
        "SELECT id FROM mesh_targets WHERE mesh_id=$1 AND target_channel=$2",
        mesh_id, channel,
    )
    if existing:
        await pool.execute("UPDATE mesh_targets SET enabled=TRUE WHERE id=$1", existing["id"])
        await message.answer(f"✅ Цель <code>{html.escape(channel)}</code> уже есть (включена).", parse_mode="HTML",
                             reply_markup=_back_to_menu())
        return
    await pool.execute(
        "INSERT INTO mesh_targets (mesh_id, target_channel) VALUES ($1, $2)",
        mesh_id, channel,
    )
    mesh = await pool.fetchrow("SELECT * FROM content_meshes WHERE id=$1", mesh_id)
    name = html.escape(mesh["name"]) if mesh else ""
    await message.answer(
        f"✅ Цель <code>{html.escape(channel)}</code> добавлена в «{name}».",
        parse_mode="HTML",
        reply_markup=_back_to_menu(),
    )


@router.callback_query(ContentMeshCb.filter(F.action == "del_target"))
async def cb_mesh_del_target(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    try:
        target_id = int(callback_data.extra)
    except (ValueError, TypeError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    await pool.execute(
        "DELETE FROM mesh_targets WHERE id=$1 AND mesh_id=$2",
        target_id, callback_data.mesh_id,
    )
    await callback.answer("🗑 Цель удалена")
    # Redirect back to targets list
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.message.edit_text("Mesh не найдена.", reply_markup=_back_to_menu())
        return
    targets = await pool.fetch(
        "SELECT * FROM mesh_targets WHERE mesh_id=$1 ORDER BY id", callback_data.mesh_id
    )
    kb = InlineKeyboardBuilder()
    for t in targets:
        status = "🟢" if t["enabled"] else "🔴"
        kb.button(
            text=f"{status} {html.escape(t['target_channel'])} ✕",
            callback_data=ContentMeshCb(action="del_target", mesh_id=callback_data.mesh_id, extra=str(t["id"])),
        )
    kb.button(text="➕ Добавить цель", callback_data=ContentMeshCb(action="add_target", mesh_id=callback_data.mesh_id))
    kb.button(text="◀️ Назад", callback_data=ContentMeshCb(action="view", mesh_id=callback_data.mesh_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📌 <b>Цели: {html.escape(mesh['name'])}</b>\n\nЦель удалена.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── settings ──────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "settings"))
async def cb_mesh_settings(
    callback: CallbackQuery, callback_data: ContentMeshCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.answer("Mesh не найдена.", show_alert=True)
        return
    await state.set_state(ContentMeshFSM.waiting_delay)
    await state.update_data(mesh_id=callback_data.mesh_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ContentMeshCb(action="view", mesh_id=callback_data.mesh_id))
    cta = html.escape(mesh["append_text"] or "—")
    await callback.message.edit_text(
        f"⚙️ <b>Настройки Mesh</b>\n\n"
        f"Задержка: <b>{mesh['delay_minutes']} мин</b>\n"
        f"Суффикс: {cta}\n\n"
        "Введите новую задержку в минутах (0–1440) и, через пробел, суффикс "
        "для добавления к каждому посту (или <code>-</code> для удаления).\n\n"
        "Пример: <code>30 Подписывайтесь: @mychannel</code>\n"
        "Без суффикса: <code>60 -</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ContentMeshFSM.waiting_delay, F.text)
async def msg_mesh_settings(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    mesh_id = data["mesh_id"]
    await state.clear()
    text = (message.text or "").strip()
    parts = text.split(" ", 1)
    if not parts[0].isdigit():
        await message.answer("⚠️ Начните с числа (минуты задержки).", parse_mode="HTML")
        return
    delay = int(parts[0])
    if not (0 <= delay <= 1440):
        await message.answer("⚠️ Задержка должна быть от 0 до 1440 минут.", parse_mode="HTML")
        return
    cta = parts[1].strip() if len(parts) > 1 else None
    if cta == "-":
        cta = None
    await pool.execute(
        "UPDATE content_meshes SET delay_minutes=$1, append_text=$2, updated_at=NOW() WHERE id=$3 AND owner_id=$4",
        delay, cta, mesh_id, message.from_user.id,
    )
    mesh = await pool.fetchrow("SELECT * FROM content_meshes WHERE id=$1", mesh_id)
    if mesh:
        await _show_mesh(message, pool, mesh, message.from_user.id, edit=False)
    else:
        await message.answer("✅ Обновлено.")


# ── logs ──────────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "logs"))
async def cb_mesh_logs(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.answer("Mesh не найдена.", show_alert=True)
        return
    rows = await pool.fetch(
        """
        SELECT mq.status, mq.scheduled_at, mq.sent_at, mq.error_msg, mt.target_channel
        FROM mesh_queue mq
        JOIN mesh_targets mt ON mt.id = mq.target_id
        WHERE mq.mesh_id=$1
        ORDER BY COALESCE(mq.sent_at, mq.scheduled_at) DESC
        LIMIT 25
        """,
        callback_data.mesh_id,
    )
    icons = {"pending": "⏳", "sent": "✅", "error": "❌"}
    if not rows:
        text = f"🕸️ <b>{html.escape(mesh['name'])} — Лог</b>\n\nОчередь пуста."
    else:
        lines = []
        for r in rows:
            ts = r["sent_at"] or r["scheduled_at"]
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            t = ts.strftime("%d.%m %H:%M") if ts else "?"
            ico = icons.get(r["status"], "?")
            tgt = html.escape(r["target_channel"])
            err = f" [{html.escape(r['error_msg'][:35])}]" if r["error_msg"] and r["status"] == "error" else ""
            lines.append(f"<code>{t}</code> {ico} {tgt}{err}")
        text = f"🕸️ <b>{html.escape(mesh['name'])} — Лог</b>\n\n" + "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ContentMeshCb(action="view", mesh_id=callback_data.mesh_id))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── delete ────────────────────────────────────────────────────────────────────


@router.callback_query(ContentMeshCb.filter(F.action == "del"))
async def cb_mesh_del(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    mesh = await _get_mesh(pool, callback_data.mesh_id, callback.from_user.id)
    if not mesh:
        await callback.answer("Mesh не найдена.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=ContentMeshCb(action="del_confirm", mesh_id=callback_data.mesh_id))
    kb.button(text="◀️ Отмена", callback_data=ContentMeshCb(action="view", mesh_id=callback_data.mesh_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚠️ Удалить Mesh <b>{html.escape(mesh['name'])}</b>?\n\n"
        "Все цели и история доставок будут удалены.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ContentMeshCb.filter(F.action == "del_confirm"))
async def cb_mesh_del_confirm(
    callback: CallbackQuery, callback_data: ContentMeshCb, pool: asyncpg.Pool
) -> None:
    await pool.execute(
        "DELETE FROM content_meshes WHERE id=$1 AND owner_id=$2",
        callback_data.mesh_id, callback.from_user.id,
    )
    await callback.answer("🗑 Удалено")
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К Content Mesh", callback_data=ContentMeshCb(action="menu"))
    await callback.message.edit_text("✅ Mesh удалена.", parse_mode="HTML", reply_markup=kb.as_markup())

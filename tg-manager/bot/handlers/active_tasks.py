"""Handler: show and cancel active background tasks for current user."""

from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import TaskCb
from services import task_registry
from bot.utils.op_helpers import safe_answer

router = Router()

_KIND_LABELS = {
    "strike": "⚔️ Удар",
    "mass_join": "👥 Массовый вступ",
    "mass_leave": "🚪 Массовый выход",
    "mass_report": "🚨 Массовая жалоба",
    "warmup": "🔥 Прогрев аккаунта",
    "scan": "🔍 Сканирование",
    "publish": "📢 Публикация",
    "dm_campaign": "✉️ Рассылка в личку",
    "bulk_edit": "✏️ Массовое редактирование",
    "invite": "📩 Инвайт в канал",
    "global_presence": "🌍 Присутствие в чатах",
    "bulk_post": "📤 Публикация / рассылка",
    "bulk_leave": "🚪 Массовый выход",
}


def _build_text_and_kb(user_id: int):
    tasks = task_registry.list_tasks(user_id)
    kb = InlineKeyboardBuilder()

    if not tasks:
        # Раньше здесь было «Все фоновые операции завершены» — неправда, если
        # в очереди идёт рассылка или вступление: их этот список не видит.
        # Плюс внутренние слова (op_worker, bulk join/leave). Теперь честно
        # говорим, что показано, и ведём туда, где видно остальное.
        text = (
            "✅ <b>Сейчас ничего не выполняется</b>\n\n"
            "Здесь видны задачи, запущенные из этого чата. Рассылки, "
            "вступления и публикации из очереди — в разделе «Операции» (/ops)."
        )
        from bot.callbacks import MassOpCb
        kb.button(text="📋 Очередь операций",
                  callback_data=MassOpCb(action="queue", op_type="all", page=0))
    else:
        lines = [f"⚡ <b>Активные задачи</b> ({len(tasks)}):\n"]
        for entry in tasks:
            kind_label = _KIND_LABELS.get(entry.kind, entry.kind)
            lines.append(
                f"• {kind_label} — <i>{entry.label}</i> [{entry.elapsed_str()}]"
            )
            kb.button(
                text=f"🛑 Отменить: {kind_label}",
                callback_data=TaskCb(action="cancel", task_id=entry.task_id),
            )
        text = "\n".join(lines)
        text += (
            "\n\n<i>♻️ Если бот перезапустится, операции из очереди продолжатся "
            "сами. Прогрев — в течение часа.</i>"
        )
        if len(tasks) > 1:
            kb.button(text="🛑 Отменить всё", callback_data=TaskCb(action="cancel_all"))

    from bot.callbacks import BmCb

    kb.button(text="🔄 Обновить", callback_data=TaskCb(action="list"))
    kb.button(text="🏠 Главное меню", callback_data=BmCb(action="main"))
    kb.adjust(1)
    return text, kb.as_markup()


@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    text, kb = _build_text_and_kb(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(TaskCb.filter(F.action == "list"))
async def cb_task_list(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    text, kb = _build_text_and_kb(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(TaskCb.filter(F.action == "cancel"))
async def cb_task_cancel(callback: CallbackQuery, callback_data: TaskCb) -> None:
    task_id = callback_data.task_id or ""
    cancelled = task_registry.cancel_task(callback.from_user.id, task_id)
    if cancelled:
        await callback.answer("✅ Задача отменена", show_alert=False)
    else:
        await callback.answer("Задача уже завершена", show_alert=False)
    text, kb = _build_text_and_kb(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(TaskCb.filter(F.action == "cancel_all"))
async def cb_task_cancel_all(callback: CallbackQuery) -> None:
    count = task_registry.cancel_all(callback.from_user.id)
    await callback.answer(f"🛑 Отменено задач: {count}", show_alert=True)
    text, kb = _build_text_and_kb(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

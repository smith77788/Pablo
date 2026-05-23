"""CRM tags and automation rules management."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.state import State, StatesGroup
import asyncpg
from bot.callbacks import CrmCb, AutoCb, BotCb, BroadcastCb
from bot.keyboards import (
    crm_menu, tag_detail_menu, automation_menu,
    automation_trigger_menu, automation_action_menu, back_to_bot,
)
from database import db

router = Router()


class AddAutoRule(StatesGroup):
    choosing_trigger = State()
    waiting_trigger_value = State()
    choosing_action = State()
    waiting_action_value = State()
    waiting_name = State()


@router.callback_query(CrmCb.filter(F.action == "menu"))
async def cb_crm_menu(callback: CallbackQuery, callback_data: CrmCb,
                       pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    tags = await db.get_tag_names(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"🏷 <b>CRM — {label}</b>\n\n"
        f"Тегов: <b>{len(tags)}</b>\n\n"
        "Теги позволяют сегментировать аудиторию и настраивать автоматизацию.",
        parse_mode="HTML",
        reply_markup=crm_menu(callback_data.bot_id, tags),
    )
    await callback.answer()


@router.callback_query(CrmCb.filter(F.action == "tag_detail"))
async def cb_tag_detail(callback: CallbackQuery, callback_data: CrmCb,
                         pool: asyncpg.Pool) -> None:
    user_ids = await db.get_users_by_tag(pool, callback_data.bot_id, callback_data.tag)
    await callback.message.edit_text(
        f"🏷 <b>Тег: {callback_data.tag}</b>\n\n"
        f"Пользователей: <b>{len(user_ids)}</b>",
        parse_mode="HTML",
        reply_markup=tag_detail_menu(callback_data.bot_id, callback_data.tag),
    )
    await callback.answer()


@router.callback_query(CrmCb.filter(F.action == "delete_tag_all"))
async def cb_delete_tag_all(callback: CallbackQuery, callback_data: CrmCb,
                              pool: asyncpg.Pool) -> None:
    await pool.execute(
        "DELETE FROM user_tags WHERE bot_id=$1 AND tag=$2",
        callback_data.bot_id, callback_data.tag,
    )
    tags = await db.get_tag_names(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"🏷 <b>CRM</b>\n\nТег «{callback_data.tag}» удалён у всех пользователей.",
        parse_mode="HTML",
        reply_markup=crm_menu(callback_data.bot_id, tags),
    )
    await callback.answer(f"🗑 Тег «{callback_data.tag}» удалён.")


# ── Automation Rules ───────────────────────────────────────────────────────

@router.callback_query(AutoCb.filter(F.action == "menu"))
async def cb_auto_menu(callback: CallbackQuery, callback_data: AutoCb,
                        pool: asyncpg.Pool) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"⚙️ <b>Автоматизация — {label}</b>\n\n"
        f"Активных правил: <b>{sum(1 for r in rules if r['is_active'])}</b> из {len(rules)}\n\n"
        "Правила выполняются автоматически при наступлении триггера.",
        parse_mode="HTML",
        reply_markup=automation_menu(callback_data.bot_id, rules),
    )
    await callback.answer()


@router.callback_query(AutoCb.filter(F.action == "view"))
async def cb_auto_view(callback: CallbackQuery, callback_data: AutoCb,
                        pool: asyncpg.Pool) -> None:
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    rule = next((r for r in rules if r["id"] == callback_data.rule_id), None)
    if not rule:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import AutoCb as AC
    kb = InlineKeyboardBuilder()
    toggle_text = "❌ Отключить" if rule["is_active"] else "✅ Включить"
    kb.button(text=toggle_text, callback_data=AC(action="toggle", bot_id=callback_data.bot_id, rule_id=rule["id"]))
    kb.button(text="🗑 Удалить", callback_data=AC(action="delete", bot_id=callback_data.bot_id, rule_id=rule["id"]))
    kb.button(text="◀️ Назад", callback_data=AC(action="menu", bot_id=callback_data.bot_id))
    kb.adjust(1)
    trig_val = f" [{rule['trigger_value']}]" if rule.get("trigger_value") else ""
    await callback.message.edit_text(
        f"⚙️ <b>Правило: {rule['name']}</b>\n\n"
        f"Статус: {'✅ Активно' if rule['is_active'] else '❌ Отключено'}\n"
        f"Триггер: <code>{rule['trigger_type']}{trig_val}</code>\n"
        f"Действие: <code>{rule['action_type']}</code>\n"
        f"Значение: <code>{rule['action_value'][:100]}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(AutoCb.filter(F.action == "toggle"))
async def cb_auto_toggle(callback: CallbackQuery, callback_data: AutoCb,
                          pool: asyncpg.Pool) -> None:
    await db.toggle_automation_rule(pool, callback_data.rule_id, callback_data.bot_id)
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"⚙️ <b>Автоматизация</b>\n\nПравил: {len(rules)}",
        parse_mode="HTML",
        reply_markup=automation_menu(callback_data.bot_id, rules),
    )
    await callback.answer("✅ Статус изменён.")


@router.callback_query(AutoCb.filter(F.action == "delete"))
async def cb_auto_delete(callback: CallbackQuery, callback_data: AutoCb,
                          pool: asyncpg.Pool) -> None:
    await db.delete_automation_rule(pool, callback_data.rule_id, callback_data.bot_id)
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"⚙️ <b>Автоматизация</b>\n\nПравил: {len(rules)}",
        parse_mode="HTML",
        reply_markup=automation_menu(callback_data.bot_id, rules),
    )
    await callback.answer("🗑 Правило удалено.")


@router.callback_query(AutoCb.filter(F.action == "add"))
async def cb_auto_add(callback: CallbackQuery, callback_data: AutoCb,
                       state: FSMContext) -> None:
    await state.set_state(AddAutoRule.choosing_trigger)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "⚙️ <b>Новое правило — шаг 1/3</b>\n\nВыберите тип триггера:",
        parse_mode="HTML",
        reply_markup=automation_trigger_menu(callback_data.bot_id),
    )
    await callback.answer()


TRIGGER_LABELS = {
    "message_received": "📩 Сообщение получено",
    "user_joined": "👤 Новый пользователь",
    "keyword": "🔑 Ключевое слово",
    "tag_added": "🏷 Тег добавлен",
}

ACTION_LABELS = {
    "send_message": "💬 Отправить сообщение",
    "add_tag": "🏷 Добавить тег",
    "remove_tag": "🗑 Удалить тег",
    "subscribe_funnel": "🔗 Подписать на цепочку",
}


async def _set_trigger(callback: CallbackQuery, state: FSMContext,
                        trigger_type: str, needs_value: bool) -> None:
    await state.update_data(trigger_type=trigger_type, trigger_value=None)
    if needs_value:
        await state.set_state(AddAutoRule.waiting_trigger_value)
        hint = "ключевое слово" if trigger_type == "keyword" else "название тега"
        await callback.message.edit_text(
            f"⚙️ Триггер: <b>{TRIGGER_LABELS[trigger_type]}</b>\n\nВведите {hint}:",
            parse_mode="HTML",
        )
    else:
        await state.set_state(AddAutoRule.choosing_action)
        data = await state.get_data()
        bot_id = data["bot_id"]
        await callback.message.edit_text(
            f"⚙️ Триггер: <b>{TRIGGER_LABELS[trigger_type]}</b>\n\n"
            "<b>Шаг 2/3</b> — Выберите действие:",
            parse_mode="HTML",
            reply_markup=automation_action_menu(bot_id),
        )
    await callback.answer()


@router.callback_query(AutoCb.filter(F.action == "trig_message"))
async def cb_trig_message(callback: CallbackQuery, callback_data: AutoCb, state: FSMContext) -> None:
    await _set_trigger(callback, state, "message_received", False)

@router.callback_query(AutoCb.filter(F.action == "trig_joined"))
async def cb_trig_joined(callback: CallbackQuery, callback_data: AutoCb, state: FSMContext) -> None:
    await _set_trigger(callback, state, "user_joined", False)

@router.callback_query(AutoCb.filter(F.action == "trig_keyword"))
async def cb_trig_keyword(callback: CallbackQuery, callback_data: AutoCb, state: FSMContext) -> None:
    await _set_trigger(callback, state, "keyword", True)

@router.callback_query(AutoCb.filter(F.action == "trig_tag"))
async def cb_trig_tag(callback: CallbackQuery, callback_data: AutoCb, state: FSMContext) -> None:
    await _set_trigger(callback, state, "tag_added", True)


@router.message(AddAutoRule.waiting_trigger_value)
async def msg_trigger_value(message: Message, state: FSMContext) -> None:
    await state.update_data(trigger_value=message.text.strip())
    await state.set_state(AddAutoRule.choosing_action)
    data = await state.get_data()
    bot_id = data["bot_id"]
    await message.answer(
        "⚙️ <b>Шаг 2/3</b> — Выберите действие:",
        parse_mode="HTML",
        reply_markup=automation_action_menu(bot_id),
    )


@router.callback_query(AutoCb.filter(F.action.in_({"act_send", "act_add_tag", "act_remove_tag"})))
async def cb_choose_action(callback: CallbackQuery, callback_data: AutoCb, state: FSMContext) -> None:
    action_map = {"act_send": "send_message", "act_add_tag": "add_tag", "act_remove_tag": "remove_tag"}
    action_type = action_map[callback_data.action]
    await state.update_data(action_type=action_type)
    await state.set_state(AddAutoRule.waiting_action_value)
    hints = {
        "send_message": "текст сообщения (HTML поддерживается)",
        "add_tag": "название тега для добавления",
        "remove_tag": "название тега для удаления",
    }
    await callback.message.edit_text(
        f"⚙️ Действие: <b>{ACTION_LABELS[action_type]}</b>\n\n"
        f"<b>Шаг 3/3</b> — Введите {hints[action_type]}:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AddAutoRule.waiting_action_value)
async def msg_action_value(message: Message, state: FSMContext) -> None:
    await state.update_data(action_value=message.text.strip())
    await state.set_state(AddAutoRule.waiting_name)
    await message.answer(
        "✅ Почти готово!\n\nВведите название для этого правила (для вашего удобства):",
    )


@router.message(AddAutoRule.waiting_name)
async def msg_rule_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    await state.clear()
    rule_id = await db.add_automation_rule(
        pool, data["bot_id"], message.text.strip(),
        data["trigger_type"], data.get("trigger_value"),
        data["action_type"], data["action_value"],
    )
    trigger_label = TRIGGER_LABELS.get(data["trigger_type"], data["trigger_type"])
    action_label = ACTION_LABELS.get(data["action_type"], data["action_type"])
    await message.answer(
        f"✅ <b>Правило создано!</b>\n\n"
        f"Название: {message.text.strip()}\n"
        f"Триггер: {trigger_label}\n"
        f"Действие: {action_label}\n"
        f"Значение: <code>{data['action_value'][:60]}</code>",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )

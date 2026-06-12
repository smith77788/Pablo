"""CRM tags, automation rules, deal pipeline and contact management."""

import csv
import io
import logging

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg
from bot.callbacks import CrmCb, AutoCb, BmCb
from bot.keyboards import (
    crm_menu,
    tag_detail_menu,
    automation_menu,
    automation_trigger_menu,
    automation_action_menu,
    automation_funnel_select,
    back_to_bot,
    subscription_locked_markup,
)
from bot.utils.subscription import require_plan, locked_text
from database import db

router = Router()
log = logging.getLogger(__name__)

_PIPELINE_STAGES = ["new", "contacted", "qualified", "won", "lost"]
_STAGE_LABELS = {
    "new": "🆕 Новый",
    "contacted": "📞 Контакт",
    "qualified": "✅ Квал-н",
    "won": "🏆 Выигран",
    "lost": "❌ Проигран",
}


class AddAutoRule(StatesGroup):
    choosing_trigger = State()
    waiting_trigger_value = State()
    choosing_action = State()
    waiting_action_value = State()
    waiting_name = State()


class AddDeal(StatesGroup):
    waiting_title = State()
    waiting_contact = State()
    waiting_value = State()


class AddDealNote(StatesGroup):
    waiting_note = State()


class AddGlobalTag(StatesGroup):
    waiting_name = State()


@router.callback_query(CrmCb.filter(F.action == "menu"))
async def cb_crm_menu(
    callback: CallbackQuery, callback_data: CrmCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.message.edit_text(
            locked_text("CRM & автоматизация", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="main")
            ),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await callback.message.edit_text(
            "❌ Бот не найден.", reply_markup=kb.as_markup()
        )
        return
    tags = await db.get_tag_names(pool, callback_data.bot_id)
    label = (
        f"@{row['username']}"
        if row["username"]
        else (row["first_name"] or str(row["bot_id"]))
    )
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if not tags:
        empty_hint = (
            "📭 <b>Тегов пока нет</b>\n\n"
            "Теги позволяют сегментировать пользователей бота — например «покупатель», «VIP», «новичок».\n\n"
            "Нажмите <b>➕ Новый тег</b> чтобы создать первый тег."
        )
        await callback.message.edit_text(
            f"🏷 <b>CRM — {safe_label}</b>\n\n{empty_hint}",
            parse_mode="HTML",
            reply_markup=crm_menu(callback_data.bot_id, tags),
        )
        return
    await callback.message.edit_text(
        f"🏷 <b>CRM — {safe_label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "CRM — это система для разделения ваших пользователей на группы. Вы вешаете «теги» (метки) на разных людей — например «покупатель», «VIP», «новичок» — и потом делаете рассылки именно для них.\n\n"
        "💡 <b>Также здесь:</b>\n"
        "• <b>Автоматизация</b> — бот сам выполняет действия при наступлении события (новый пользователь → отправить сообщение, написал слово → добавить тег)\n\n"
        f"Тегов создано: <b>{len(tags)}</b>",
        parse_mode="HTML",
        reply_markup=crm_menu(callback_data.bot_id, tags),
    )


@router.callback_query(CrmCb.filter(F.action == "add_tag_global"))
async def cb_add_tag_global(
    callback: CallbackQuery, callback_data: CrmCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AddGlobalTag.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Отмена",
        callback_data=CrmCb(action="menu", bot_id=callback_data.bot_id),
    )
    await callback.message.edit_text(
        "🏷 <b>Создать тег</b>\n\n"
        "Введите название тега (латиница, кириллица, цифры, _ допустимы).\n"
        "Тег будет создан и доступен для назначения пользователям через автоматизацию.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AddGlobalTag.waiting_name, F.text)
async def msg_global_tag_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    tag = (message.text or "").strip()
    if not tag or len(tag) > 50:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена",
            callback_data=CrmCb(action="menu", bot_id=data.get("bot_id", 0)),
        )
        await message.answer(
            "⚠️ Название тега должно быть от 1 до 50 символов. Введите снова:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.clear()
    safe_tag = tag.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Use user_id=0 as a sentinel to create a standalone tag definition.
    # This avoids polluting the segment with the owner's Telegram user_id,
    # which is not a bot user. get_users_by_tag excludes user_id=0 so
    # broadcasts to this segment will correctly target only real bot users
    # who later get the tag applied via automation rules.
    await db.add_user_tag(pool, data["bot_id"], 0, tag)
    tags = await db.get_tag_names(pool, data["bot_id"])
    await message.answer(
        f"✅ Тег <b>{safe_tag}</b> создан!\n\nВсего тегов в боте: <b>{len(tags)}</b>",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


@router.callback_query(CrmCb.filter(F.action == "tag_detail"))
async def cb_tag_detail(
    callback: CallbackQuery, callback_data: CrmCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    tag = callback_data.tag or ""
    safe_tag = tag.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    user_ids = await db.get_users_by_tag(pool, callback_data.bot_id, tag)
    await callback.message.edit_text(
        f"🏷 <b>Тег: {safe_tag}</b>\n\nПользователей: <b>{len(user_ids)}</b>",
        parse_mode="HTML",
        reply_markup=tag_detail_menu(callback_data.bot_id, tag),
    )


@router.callback_query(CrmCb.filter(F.action == "delete_tag_confirm"))
async def cb_delete_tag_confirm(
    callback: CallbackQuery, callback_data: CrmCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    tag = callback_data.tag or ""
    safe_tag = tag.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    user_ids = await db.get_users_by_tag(pool, callback_data.bot_id, tag)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Да, удалить",
        callback_data=CrmCb(
            action="delete_tag_all", bot_id=callback_data.bot_id, tag=tag
        ),
    )
    kb.button(
        text="◀️ Отмена",
        callback_data=CrmCb(action="tag_detail", bot_id=callback_data.bot_id, tag=tag),
    )
    kb.adjust(2)
    await callback.message.edit_text(
        f"⚠️ <b>Подтвердите удаление тега</b>\n\n"
        f"Тег: <b>{safe_tag}</b>\n"
        f"Пользователей с этим тегом: <b>{len(user_ids)}</b>\n\n"
        "Тег будет удалён у всех пользователей. Это действие необратимо.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CrmCb.filter(F.action == "delete_tag_all"))
async def cb_delete_tag_all(
    callback: CallbackQuery, callback_data: CrmCb, pool: asyncpg.Pool
) -> None:
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    tag = callback_data.tag or ""
    await callback.answer(f"🗑 Тег «{tag}» удалён.")
    try:
        await pool.execute(
            "DELETE FROM user_tags WHERE bot_id=$1 AND tag=$2",
            callback_data.bot_id,
            tag,
        )
    except Exception:
        log.warning("crm: tag delete DB error", exc_info=True)
    safe_tag = tag.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    tags = await db.get_tag_names(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"🏷 <b>CRM</b>\n\nТег «{safe_tag}» удалён у всех пользователей.",
        parse_mode="HTML",
        reply_markup=crm_menu(callback_data.bot_id, tags),
    )


# ── Automation Rules ───────────────────────────────────────────────────────


@router.callback_query(AutoCb.filter(F.action == "menu"))
async def cb_auto_menu(
    callback: CallbackQuery, callback_data: AutoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.message.edit_text(
            locked_text("Автоматизация", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "starter", back_callback=BmCb(action="main")
            ),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await callback.message.edit_text(
            "❌ Бот не найден.", reply_markup=kb.as_markup()
        )
        return
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    label = (
        f"@{row['username']}"
        if row["username"]
        else (row["first_name"] or str(row["bot_id"]))
    )
    safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if not rules:
        await callback.message.edit_text(
            f"⚙️ <b>Автоматизация — {safe_label}</b>\n\n"
            "📭 <b>Правил пока нет</b>\n\n"
            "Правила автоматически выполняют действия при наступлении триггера — например, отправляют сообщение новому пользователю или добавляют тег по ключевому слову.\n\n"
            "Нажмите <b>➕ Новое правило</b> чтобы создать первое.",
            parse_mode="HTML",
            reply_markup=automation_menu(callback_data.bot_id, rules),
        )
        return
    await callback.message.edit_text(
        f"⚙️ <b>Автоматизация — {safe_label}</b>\n\n"
        f"Активных правил: <b>{sum(1 for r in rules if r['is_active'])}</b> из {len(rules)}\n\n"
        "Правила выполняются автоматически при наступлении триггера.",
        parse_mode="HTML",
        reply_markup=automation_menu(callback_data.bot_id, rules),
    )


@router.callback_query(AutoCb.filter(F.action == "view"))
async def cb_auto_view(
    callback: CallbackQuery, callback_data: AutoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    rule = next((r for r in rules if r["id"] == callback_data.rule_id), None)
    if not rule:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад",
            callback_data=AutoCb(action="menu", bot_id=callback_data.bot_id),
        )
        await callback.message.edit_text(
            "❌ Правило не найдено.", reply_markup=kb.as_markup()
        )
        return
    kb = InlineKeyboardBuilder()
    toggle_text = "❌ Отключить" if rule["is_active"] else "✅ Включить"
    kb.button(
        text=toggle_text,
        callback_data=AutoCb(
            action="toggle", bot_id=callback_data.bot_id, rule_id=rule["id"]
        ),
    )
    kb.button(
        text="🗑 Удалить",
        callback_data=AutoCb(
            action="delete_confirm", bot_id=callback_data.bot_id, rule_id=rule["id"]
        ),
    )
    kb.button(
        text="◀️ Назад", callback_data=AutoCb(action="menu", bot_id=callback_data.bot_id)
    )
    kb.adjust(1)
    trig_val = f" [{rule['trigger_value']}]" if rule.get("trigger_value") else ""
    safe_name = (
        rule["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    safe_trig_val = (
        trig_val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    safe_action_val = (
        rule["action_value"][:100]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    await callback.message.edit_text(
        f"⚙️ <b>Правило: {safe_name}</b>\n\n"
        f"Статус: {'✅ Активно' if rule['is_active'] else '❌ Отключено'}\n"
        f"Триггер: <code>{rule['trigger_type']}{safe_trig_val}</code>\n"
        f"Действие: <code>{rule['action_type']}</code>\n"
        f"Значение: <code>{safe_action_val}</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoCb.filter(F.action == "toggle"))
async def cb_auto_toggle(
    callback: CallbackQuery, callback_data: AutoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("✅ Статус изменён.")
    await db.toggle_automation_rule(pool, callback_data.rule_id, callback_data.bot_id)
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"⚙️ <b>Автоматизация</b>\n\nПравил: {len(rules)}",
        parse_mode="HTML",
        reply_markup=automation_menu(callback_data.bot_id, rules),
    )


@router.callback_query(AutoCb.filter(F.action == "delete_confirm"))
async def cb_auto_delete_confirm(
    callback: CallbackQuery, callback_data: AutoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    rule = next((r for r in rules if r["id"] == callback_data.rule_id), None)
    safe_name = (
        rule["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if rule
        else str(callback_data.rule_id)
    )
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Да, удалить",
        callback_data=AutoCb(
            action="delete", bot_id=callback_data.bot_id, rule_id=callback_data.rule_id
        ),
    )
    kb.button(
        text="◀️ Отмена",
        callback_data=AutoCb(
            action="view", bot_id=callback_data.bot_id, rule_id=callback_data.rule_id
        ),
    )
    kb.adjust(2)
    await callback.message.edit_text(
        f"⚠️ <b>Подтвердите удаление правила</b>\n\n"
        f"Правило: <b>{safe_name}</b>\n\n"
        "Правило будет удалено без возможности восстановления.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoCb.filter(F.action == "delete"))
async def cb_auto_delete(
    callback: CallbackQuery, callback_data: AutoCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("🗑 Правило удалено.")
    await db.delete_automation_rule(pool, callback_data.rule_id, callback_data.bot_id)
    rules = await db.get_automation_rules(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"⚙️ <b>Автоматизация</b>\n\nПравил: {len(rules)}",
        parse_mode="HTML",
        reply_markup=automation_menu(callback_data.bot_id, rules),
    )


@router.callback_query(AutoCb.filter(F.action == "add"))
async def cb_auto_add(
    callback: CallbackQuery, callback_data: AutoCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AddAutoRule.choosing_trigger)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "⚙️ <b>Новое правило — шаг 1/3</b>\n\nВыберите тип триггера:",
        parse_mode="HTML",
        reply_markup=automation_trigger_menu(callback_data.bot_id),
    )


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


async def _set_trigger(
    callback: CallbackQuery, state: FSMContext, trigger_type: str, needs_value: bool
) -> None:
    await callback.answer()
    await state.update_data(trigger_type=trigger_type, trigger_value=None)
    data = await state.get_data()
    bot_id = data.get("bot_id", 0)
    if needs_value:
        await state.set_state(AddAutoRule.waiting_trigger_value)
        hint = "ключевое слово" if trigger_type == "keyword" else "название тега"
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
        await callback.message.edit_text(
            f"⚙️ Триггер: <b>{TRIGGER_LABELS[trigger_type]}</b>\n\nВведите {hint}:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
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


@router.callback_query(AutoCb.filter(F.action == "trig_message"))
async def cb_trig_message(
    callback: CallbackQuery, callback_data: AutoCb, state: FSMContext
) -> None:
    await _set_trigger(callback, state, "message_received", False)


@router.callback_query(AutoCb.filter(F.action == "trig_joined"))
async def cb_trig_joined(
    callback: CallbackQuery, callback_data: AutoCb, state: FSMContext
) -> None:
    await _set_trigger(callback, state, "user_joined", False)


@router.callback_query(AutoCb.filter(F.action == "trig_keyword"))
async def cb_trig_keyword(
    callback: CallbackQuery, callback_data: AutoCb, state: FSMContext
) -> None:
    await _set_trigger(callback, state, "keyword", True)


@router.callback_query(AutoCb.filter(F.action == "trig_tag"))
async def cb_trig_tag(
    callback: CallbackQuery, callback_data: AutoCb, state: FSMContext
) -> None:
    await _set_trigger(callback, state, "tag_added", True)


@router.message(AddAutoRule.waiting_trigger_value, F.text)
async def msg_trigger_value(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    bot_id = data.get("bot_id", 0)
    if not value or len(value) > 100:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
        await message.answer(
            "⚠️ Значение должно быть от 1 до 100 символов. Введите снова:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(trigger_value=value)
    await state.set_state(AddAutoRule.choosing_action)
    await message.answer(
        "⚙️ <b>Шаг 2/3</b> — Выберите действие:",
        parse_mode="HTML",
        reply_markup=automation_action_menu(bot_id),
    )


@router.callback_query(
    AutoCb.filter(F.action.in_({"act_send", "act_add_tag", "act_remove_tag"}))
)
async def cb_choose_action(
    callback: CallbackQuery, callback_data: AutoCb, state: FSMContext
) -> None:
    await callback.answer()
    action_map = {
        "act_send": "send_message",
        "act_add_tag": "add_tag",
        "act_remove_tag": "remove_tag",
    }
    action_type = action_map[callback_data.action]
    await state.update_data(action_type=action_type)
    await state.set_state(AddAutoRule.waiting_action_value)
    data = await state.get_data()
    bot_id = data.get("bot_id", 0)
    hints = {
        "send_message": "текст сообщения (HTML поддерживается)",
        "add_tag": "название тега для добавления",
        "remove_tag": "название тега для удаления",
    }
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
    await callback.message.edit_text(
        f"⚙️ Действие: <b>{ACTION_LABELS[action_type]}</b>\n\n"
        f"<b>Шаг 3/3</b> — Введите {hints[action_type]}:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(
    AutoCb.filter(F.action == "act_funnel"), AddAutoRule.choosing_action
)
async def cb_act_funnel(
    callback: CallbackQuery,
    callback_data: AutoCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    data = await state.get_data()
    bot_id = data.get("bot_id", callback_data.bot_id)
    await state.update_data(action_type="subscribe_funnel")
    funnels = await db.get_funnels(pool, bot_id)
    if not funnels:
        await callback.message.edit_text(
            "❌ У этого бота нет цепочек.\n\nСначала создайте цепочку в разделе «Цепочки».",
            reply_markup=back_to_bot(bot_id),
        )
        await state.clear()
        return
    await callback.message.edit_text(
        "🔗 <b>Шаг 3/3</b> — Выберите цепочку для подписки:",
        parse_mode="HTML",
        reply_markup=automation_funnel_select(bot_id, funnels),
    )


@router.callback_query(
    AutoCb.filter(F.action == "sel_funnel"), AddAutoRule.choosing_action
)
async def cb_sel_funnel(
    callback: CallbackQuery,
    callback_data: AutoCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    funnel_id = callback_data.rule_id
    funnels = await db.get_funnels(pool, callback_data.bot_id)
    funnel = next((f for f in funnels if f["id"] == funnel_id), None)
    funnel_name = funnel["name"] if funnel else str(funnel_id)
    safe_name = (
        funnel_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    await state.update_data(action_value=str(funnel_id))
    await state.set_state(AddAutoRule.waiting_name)
    data = await state.get_data()
    bot_id = data.get("bot_id", callback_data.bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
    await callback.message.edit_text(
        f"✅ Цепочка: <b>{safe_name}</b>\n\nВведите название для этого правила:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AddAutoRule.waiting_action_value, F.text)
async def msg_action_value(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    data = await state.get_data()
    bot_id = data.get("bot_id", 0)
    if not value or len(value) > 500:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
        await message.answer(
            "⚠️ Текст должен быть от 1 до 500 символов. Введите снова:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(action_value=value)
    await state.set_state(AddAutoRule.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
    await message.answer(
        "✅ Почти готово!\n\nВведите название для этого правила (для вашего удобства):",
        reply_markup=kb.as_markup(),
    )


@router.message(AddAutoRule.waiting_name, F.text)
async def msg_rule_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    rule_name = (message.text or "").strip()
    if not rule_name or len(rule_name) > 100:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена",
            callback_data=AutoCb(action="menu", bot_id=data.get("bot_id", 0)),
        )
        await message.answer(
            "⚠️ Название правила должно быть от 1 до 100 символов. Введите снова:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.clear()
    await db.add_automation_rule(
        pool,
        data["bot_id"],
        rule_name,
        data["trigger_type"],
        data.get("trigger_value"),
        data["action_type"],
        data["action_value"],
    )
    trigger_label = TRIGGER_LABELS.get(data["trigger_type"], data["trigger_type"])
    action_label = ACTION_LABELS.get(data["action_type"], data["action_type"])
    await message.answer(
        f"✅ <b>Правило создано!</b>\n\n"
        f"Название: {rule_name}\n"
        f"Триггер: {trigger_label}\n"
        f"Действие: {action_label}\n"
        f"Значение: <code>{data['action_value'][:60]}</code>",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


# ══════════════════════════════════════════════════════════════════
# CRM DASHBOARD — real counts from DB
# ══════════════════════════════════════════════════════════════════


def _crm_dashboard_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="💼 Воронка сделок", callback_data=CrmCb(action="pipeline"))
    kb.button(text="➕ Новая сделка", callback_data=CrmCb(action="deal_add"))
    kb.button(text="📥 Импорт CSV", callback_data=CrmCb(action="csv_import_prompt"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1, 2, 1)
    return kb


@router.callback_query(CrmCb.filter(F.action == "dashboard"))
async def cb_crm_dashboard(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    stats = await db.get_crm_dashboard_stats(pool, owner_id)
    total_deals = sum(v["count"] for v in stats.values())
    won_val = stats["won"]["value"]

    lines = [
        "💼 <b>CRM — Дашборд</b>\n",
        f"Всего сделок: <b>{total_deals}</b>",
        f"Выигранных: <b>{stats['won']['count']}</b>  (💰 {won_val:,.0f})",
        f"Проигранных: <b>{stats['lost']['count']}</b>",
        "",
        "<b>По стадиям:</b>",
    ]
    for stage in _PIPELINE_STAGES:
        cnt = stats[stage]["count"]
        label = _STAGE_LABELS[stage]
        lines.append(f"  {label}: <b>{cnt}</b>")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_crm_dashboard_kb().as_markup(),
    )


# ── Pipeline view ─────────────────────────────────────────────────────────────


@router.callback_query(CrmCb.filter(F.action == "pipeline"))
async def cb_crm_pipeline(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id

    deals = await db.get_crm_deals(pool, owner_id)
    if not deals:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Новая сделка", callback_data=CrmCb(action="deal_add"))
        kb.button(text="◀️ Назад", callback_data=CrmCb(action="dashboard"))
        kb.adjust(1)
        await callback.message.edit_text(
            "💼 <b>Воронка сделок</b>\n\n📭 Сделок пока нет. Создайте первую.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    by_stage: dict = {s: [] for s in _PIPELINE_STAGES}
    for d in deals:
        by_stage.setdefault(d["stage"], []).append(d)

    lines = ["💼 <b>Воронка сделок</b>\n"]
    kb = InlineKeyboardBuilder()
    for stage in _PIPELINE_STAGES:
        stage_deals = by_stage.get(stage, [])
        label = _STAGE_LABELS[stage]
        lines.append(f"\n{label} — <b>{len(stage_deals)}</b>")
        for d in stage_deals[:5]:
            title = (d["title"] or "")[:28]
            lines.append(f"  • {title}")
            kb.button(
                text=f"📄 {title[:16]}",
                callback_data=CrmCb(action="deal_view", deal_id=d["id"]),
            )
        if len(stage_deals) > 5:
            lines.append(f"  <i>...ещё {len(stage_deals) - 5}</i>")

    kb.button(text="➕ Добавить сделку", callback_data=CrmCb(action="deal_add"))
    kb.button(text="◀️ Дашборд", callback_data=CrmCb(action="dashboard"))
    kb.adjust(1)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3950] + "\n\n<i>...обрезано</i>"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Deal view ─────────────────────────────────────────────────────────────────


def _deal_stage_move_kb(deal_id: int, current_stage: str) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for stage in _PIPELINE_STAGES:
        if stage != current_stage:
            label = _STAGE_LABELS[stage]
            kb.button(
                text=f"→ {label}",
                callback_data=CrmCb(action="deal_move", deal_id=deal_id, tag=stage),
            )
    kb.button(
        text="📝 Добавить заметку",
        callback_data=CrmCb(action="deal_note", deal_id=deal_id),
    )
    kb.button(
        text="🗑 Удалить",
        callback_data=CrmCb(action="deal_delete_confirm", deal_id=deal_id),
    )
    kb.button(text="◀️ Воронка", callback_data=CrmCb(action="pipeline"))
    kb.adjust(2, 2, 1, 1)
    return kb


@router.callback_query(CrmCb.filter(F.action == "deal_view"))
async def cb_deal_view(
    callback: CallbackQuery, callback_data: CrmCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    deal = await db.get_crm_deal(pool, callback_data.deal_id, callback.from_user.id)
    if not deal:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    activities = await db.get_crm_activity(pool, deal["id"], limit=5)
    stage_label = _STAGE_LABELS.get(deal["stage"], deal["stage"])
    contact = deal["contact"] or "—"
    val = f"{deal['value']:,.0f}" if deal["value"] else "—"
    notes = (deal["notes"] or "—")[:200]

    lines = [
        f"📄 <b>{deal['title']}</b>\n",
        f"Стадия: {stage_label}",
        f"Контакт: {contact}",
        f"Сумма: {val}",
        f"Заметки: <i>{notes}</i>",
    ]
    if activities:
        lines.append("\n<b>Активность:</b>")
        for act in activities:
            ts = act["created_at"].strftime("%d.%m %H:%M")
            lines.append(f"  [{ts}] {act['note'][:80]}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_deal_stage_move_kb(deal["id"], deal["stage"]).as_markup(),
    )


# ── Move deal between stages ──────────────────────────────────────────────────


@router.callback_query(CrmCb.filter(F.action == "deal_move"))
async def cb_deal_move(
    callback: CallbackQuery, callback_data: CrmCb, pool: asyncpg.Pool
) -> None:
    new_stage = callback_data.tag or "new"
    if new_stage not in _PIPELINE_STAGES:
        await callback.answer("Неверная стадия.", show_alert=True)
        return

    await db.move_crm_deal_stage(
        pool, callback_data.deal_id, callback.from_user.id, new_stage
    )
    await db.add_crm_activity(
        pool,
        callback.from_user.id,
        callback_data.deal_id,
        f"Стадия изменена на «{_STAGE_LABELS.get(new_stage, new_stage)}»",
    )
    await callback.answer(f"✅ Стадия → {_STAGE_LABELS.get(new_stage, new_stage)}")

    deal = await db.get_crm_deal(pool, callback_data.deal_id, callback.from_user.id)
    if not deal:
        await callback.message.edit_text("✅ Стадия изменена.")
        return
    activities = await db.get_crm_activity(pool, deal["id"], limit=5)
    stage_label = _STAGE_LABELS.get(deal["stage"], deal["stage"])

    lines = [
        f"📄 <b>{deal['title']}</b>\n",
        f"Стадия: {stage_label}",
        f"Контакт: {deal['contact'] or '—'}",
        f"Сумма: {deal['value']:,.0f}" if deal["value"] else "Сумма: —",
    ]
    if activities:
        lines.append("\n<b>Активность:</b>")
        for act in activities:
            ts = act["created_at"].strftime("%d.%m %H:%M")
            lines.append(f"  [{ts}] {act['note'][:80]}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_deal_stage_move_kb(deal["id"], deal["stage"]).as_markup(),
    )


# ── Add deal (FSM) ────────────────────────────────────────────────────────────


@router.callback_query(CrmCb.filter(F.action == "deal_add"))
async def cb_deal_add(
    callback: CallbackQuery, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AddDeal.waiting_title)
    await state.update_data(csv_mode=False)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=CrmCb(action="pipeline"))
    await callback.message.edit_text(
        "💼 <b>Новая сделка</b>\n\nВведите название сделки:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AddDeal.waiting_title, F.text)
async def msg_deal_title_or_csv(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    if data.get("csv_mode"):
        await state.clear()
        await _process_csv_import(message, pool, message.from_user.id, message.text or "")
        return
    title = (message.text or "").strip()
    if not title or len(title) > 120:
        await message.answer("⚠️ Название от 1 до 120 символов. Попробуйте ещё раз:")
        return
    await state.update_data(deal_title=title)
    await state.set_state(AddDeal.waiting_contact)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=CrmCb(action="deal_skip_contact"))
    await message.answer(
        "📞 Введите имя или @username контакта (или пропустите):",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CrmCb.filter(F.action == "deal_skip_contact"))
async def cb_deal_skip_contact(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(deal_contact="")
    await state.set_state(AddDeal.waiting_value)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=CrmCb(action="deal_skip_value"))
    await callback.message.edit_text(
        "💰 Введите сумму сделки (число, например 50000) или пропустите:",
        reply_markup=kb.as_markup(),
    )


@router.message(AddDeal.waiting_contact, F.text)
async def msg_deal_contact(message: Message, state: FSMContext) -> None:
    contact = (message.text or "").strip()[:80]
    await state.update_data(deal_contact=contact)
    await state.set_state(AddDeal.waiting_value)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=CrmCb(action="deal_skip_value"))
    await message.answer(
        "💰 Введите сумму сделки (число) или пропустите:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CrmCb.filter(F.action == "deal_skip_value"))
async def cb_deal_skip_value(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    await _finish_deal_creation(callback.message, state, pool, callback.from_user.id, 0.0)


@router.message(AddDeal.waiting_value, F.text)
async def msg_deal_value(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    raw = (message.text or "").strip().replace(",", ".").replace(" ", "")
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    await _finish_deal_creation(message, state, pool, message.from_user.id, value)


async def _finish_deal_creation(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    owner_id: int,
    value: float,
) -> None:
    data = await state.get_data()
    await state.clear()
    title = data.get("deal_title", "Новая сделка")
    contact = data.get("deal_contact", "")
    deal_id = await db.create_crm_deal(
        pool, owner_id, title, contact=contact, stage="new", value=value
    )
    await db.add_crm_activity(pool, owner_id, deal_id, "Сделка создана")
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📄 Открыть сделку",
        callback_data=CrmCb(action="deal_view", deal_id=deal_id),
    )
    kb.button(text="💼 Воронка", callback_data=CrmCb(action="pipeline"))
    kb.adjust(1)
    val_str = f"{value:,.0f}" if value else "не указана"
    await message.answer(
        f"✅ <b>Сделка создана!</b>\n\n"
        f"Название: <b>{title}</b>\n"
        f"Контакт: {contact or '—'}\n"
        f"Сумма: {val_str}\n"
        f"Стадия: {_STAGE_LABELS['new']}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Deal note ─────────────────────────────────────────────────────────────────


@router.callback_query(CrmCb.filter(F.action == "deal_note"))
async def cb_deal_note_prompt(
    callback: CallbackQuery, callback_data: CrmCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AddDealNote.waiting_note)
    await state.update_data(deal_id=callback_data.deal_id)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="❌ Отмена",
        callback_data=CrmCb(action="deal_view", deal_id=callback_data.deal_id),
    )
    await callback.message.edit_text(
        "📝 Введите заметку / запись активности:",
        reply_markup=kb.as_markup(),
    )


@router.message(AddDealNote.waiting_note, F.text)
async def msg_deal_note(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    note = (message.text or "").strip()
    data = await state.get_data()
    deal_id = data.get("deal_id", 0)
    await state.clear()
    if not note:
        await message.answer("⚠️ Заметка не может быть пустой.")
        return
    await db.add_crm_activity(pool, message.from_user.id, deal_id, note[:500])
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📄 К сделке",
        callback_data=CrmCb(action="deal_view", deal_id=deal_id),
    )
    await message.answer("✅ Заметка сохранена.", reply_markup=kb.as_markup())


# ── Delete deal ───────────────────────────────────────────────────────────────


@router.callback_query(CrmCb.filter(F.action == "deal_delete_confirm"))
async def cb_deal_delete_confirm(callback: CallbackQuery, callback_data: CrmCb) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Да, удалить",
        callback_data=CrmCb(action="deal_delete", deal_id=callback_data.deal_id),
    )
    kb.button(
        text="◀️ Отмена",
        callback_data=CrmCb(action="deal_view", deal_id=callback_data.deal_id),
    )
    kb.adjust(2)
    await callback.message.edit_text(
        "⚠️ <b>Удалить сделку?</b>\n\nДействие необратимо.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(CrmCb.filter(F.action == "deal_delete"))
async def cb_deal_delete(
    callback: CallbackQuery, callback_data: CrmCb, pool: asyncpg.Pool
) -> None:
    await db.delete_crm_deal(pool, callback_data.deal_id, callback.from_user.id)
    await callback.answer("🗑 Сделка удалена.")
    kb = InlineKeyboardBuilder()
    kb.button(text="💼 Воронка", callback_data=CrmCb(action="pipeline"))
    await callback.message.edit_text("✅ Сделка удалена.", reply_markup=kb.as_markup())


# ══════════════════════════════════════════════════════════════════
# CSV IMPORT
# ══════════════════════════════════════════════════════════════════


@router.callback_query(CrmCb.filter(F.action == "csv_import_prompt"))
async def cb_csv_import_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddDeal.waiting_title)
    await state.update_data(csv_mode=True)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=CrmCb(action="dashboard"))
    await callback.message.edit_text(
        "📥 <b>Импорт из CSV</b>\n\n"
        "Отправьте <b>.csv файл</b> или вставьте данные текстом.\n\n"
        "Формат (первая строка — заголовки):\n"
        "<code>title,contact,value,stage,notes</code>\n\n"
        "Допустимые стадии: new, contacted, qualified, won, lost\n"
        "Разделитель: запятая или точка с запятой.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AddDeal.waiting_title, F.document)
async def msg_csv_file(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    if not data.get("csv_mode"):
        await message.answer("⚠️ Ожидается текст названия сделки, а не файл.")
        return
    doc = message.document
    if not doc or not (doc.file_name or "").lower().endswith(".csv"):
        await message.answer("⚠️ Ожидается .csv файл.")
        return
    await state.clear()
    bot = message.bot
    file = await bot.get_file(doc.file_id)
    file_bytes = await bot.download_file(file.file_path)
    raw_text = file_bytes.read().decode("utf-8", errors="replace")
    await _process_csv_import(message, pool, message.from_user.id, raw_text)


async def _process_csv_import(
    message: Message,
    pool: asyncpg.Pool,
    owner_id: int,
    raw_text: str,
) -> None:
    """Parse CSV text and bulk-insert deals."""
    delimiter = ";" if raw_text.count(";") > raw_text.count(",") else ","
    reader = csv.DictReader(io.StringIO(raw_text), delimiter=delimiter)

    _field_map = {
        "title": "title", "название": "title",
        "contact": "contact", "контакт": "contact",
        "value": "value", "сумма": "value",
        "stage": "stage", "стадия": "stage",
        "notes": "notes", "заметки": "notes", "note": "notes",
    }

    ok = 0
    errors = 0
    for row in reader:
        norm: dict = {}
        for k, v in row.items():
            mapped = _field_map.get((k or "").strip().lower())
            if mapped:
                norm[mapped] = (v or "").strip()

        title = norm.get("title", "").strip()
        if not title:
            errors += 1
            continue

        contact = norm.get("contact", "")
        stage_raw = (norm.get("stage") or "new").lower()
        stage = stage_raw if stage_raw in _PIPELINE_STAGES else "new"
        notes = norm.get("notes", "")
        try:
            value = float(
                norm.get("value", "0").replace(",", ".").replace(" ", "") or 0
            )
        except ValueError:
            value = 0.0

        try:
            deal_id = await db.create_crm_deal(
                pool, owner_id, title,
                contact=contact, stage=stage, value=value, notes=notes,
            )
            await db.add_crm_activity(pool, owner_id, deal_id, "Импортирована из CSV")
            ok += 1
        except Exception:
            log.warning("csv import: failed to insert '%s'", title, exc_info=True)
            errors += 1

    kb = InlineKeyboardBuilder()
    kb.button(text="💼 Воронка сделок", callback_data=CrmCb(action="pipeline"))
    kb.button(text="◀️ Дашборд", callback_data=CrmCb(action="dashboard"))
    kb.adjust(1)
    await message.answer(
        f"📥 <b>Импорт завершён</b>\n\n"
        f"✅ Импортировано: <b>{ok}</b>\n"
        f"❌ Ошибок: <b>{errors}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

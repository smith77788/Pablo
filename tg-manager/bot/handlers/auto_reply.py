"""Auto-reply rules management for managed bots."""
import html as _html
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg
from bot.callbacks import AutoReplyCb, AutoCb
from bot.keyboards import auto_reply_menu, auto_reply_trigger_menu, auto_reply_view, back_to_bot, auto_reply_copy_target
from bot.states import AddAutoReply
from database import db

router = Router()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _ar_cancel_kb(bot_id: int) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoReplyCb(action="menu", bot_id=bot_id))
    return kb.as_markup()


def _ar_back_cancel_kb(bot_id: int, back_action: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=AutoReplyCb(action=back_action, bot_id=bot_id))
    kb.button(text="❌ Отмена", callback_data=AutoReplyCb(action="menu", bot_id=bot_id))
    kb.adjust(2)
    return kb.as_markup()


# ── Handlers ────────────────────────────────────────────────────────────────

@router.callback_query(AutoReplyCb.filter(F.action == "menu"))
async def cb_ar_menu(callback: CallbackQuery, callback_data: AutoReplyCb,
                     pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"🤖 <b>Авто-ответы — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Авто-ответы — это правила, по которым бот автоматически отвечает пользователям без вашего участия. Например: если написали «цена» — бот отвечает прайсом.\n\n"
        "💡 <b>Типы триггеров:</b>\n"
        "• <b>/start</b> — приветствие при первом запуске\n"
        "• <b>Ключевое слово</b> — любое слово в сообщении\n"
        "• <b>Любое сообщение</b> — отвечает на всё подряд\n\n"
        f"Правил: <b>{len(replies)}</b> | Активных: <b>{sum(1 for r in replies if r['is_active'])}</b>",
        parse_mode="HTML",
        reply_markup=auto_reply_menu(callback_data.bot_id, replies),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "add"))
async def cb_ar_add(callback: CallbackQuery, callback_data: AutoReplyCb,
                    state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AddAutoReply.choosing_trigger)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "➕ <b>Новое правило</b>\n\nВыберите тип триггера:",
        parse_mode="HTML",
        reply_markup=auto_reply_trigger_menu(callback_data.bot_id),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "trig_start"))
async def cb_trig_start(callback: CallbackQuery, callback_data: AutoReplyCb,
                        state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(trigger_type="start", keyword=None)
    await state.set_state(AddAutoReply.waiting_text)
    await callback.message.edit_text(
        "▶️ Триггер: <b>/start</b>\n\nВведите текст ответа (HTML-форматирование поддерживается):",
        parse_mode="HTML",
        reply_markup=_ar_cancel_kb(callback_data.bot_id),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "trig_keyword"))
async def cb_trig_keyword(callback: CallbackQuery, callback_data: AutoReplyCb,
                          state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(trigger_type="keyword")
    await state.set_state(AddAutoReply.waiting_keyword)
    await callback.message.edit_text(
        "🔑 Триггер: <b>Ключевое слово</b>\n\n"
        "Введите ключевое слово (регистр не важен).\n\n"
        "💡 <b>Как работает:</b>\n"
        "• Бот проверяет, содержит ли сообщение ваше ключевое слово\n"
        "• Например, ключ «<code>цена</code>» сработает на «Какая цена?»\n"
        "• Можно использовать фразу: «<code>как заказать</code>»\n"
        "• Русский и английский язык поддерживаются",
        parse_mode="HTML",
        reply_markup=_ar_cancel_kb(callback_data.bot_id),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "trig_any"))
async def cb_trig_any(callback: CallbackQuery, callback_data: AutoReplyCb,
                      state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(trigger_type="any", keyword=None)
    await state.set_state(AddAutoReply.waiting_text)
    await callback.message.edit_text(
        "💬 Триггер: <b>Любое сообщение</b>\n\nВведите текст ответа (HTML-форматирование поддерживается):",
        parse_mode="HTML",
        reply_markup=_ar_cancel_kb(callback_data.bot_id),
    )


@router.message(AddAutoReply.waiting_keyword, F.text)
async def msg_ar_keyword(message: Message, state: FSMContext) -> None:
    keyword = message.text.strip()
    if not keyword:
        data = await state.get_data()
        await message.answer("⚠️ Ключевое слово не может быть пустым. Введите снова:", reply_markup=_ar_cancel_kb(data.get("bot_id", 0)))
        return
    if len(keyword) > 100:
        await message.answer("⚠️ Слишком длинное ключевое слово (макс. 100 символов). Введите снова:", reply_markup=_ar_cancel_kb((await state.get_data()).get("bot_id", 0)))
        return
    await state.update_data(keyword=keyword)
    await state.set_state(AddAutoReply.waiting_text)
    data = await state.get_data()
    await message.answer(
        f"🔑 Ключевое слово: <code>{keyword}</code>\n\n"
        "Введите текст ответа (HTML-форматирование поддерживается):",
        parse_mode="HTML",
        reply_markup=_ar_cancel_kb(data.get("bot_id", 0)),
    )


@router.message(AddAutoReply.waiting_text, F.text)
async def msg_ar_text(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text = message.text.strip()
    if not text:
        data = await state.get_data()
        await message.answer("⚠️ Текст ответа не может быть пустым. Введите снова:", reply_markup=_ar_cancel_kb(data.get("bot_id", 0)))
        return
    data = await state.get_data()
    await state.clear()
    await db.add_auto_reply(
        pool, data["bot_id"], data["trigger_type"],
        data.get("keyword"), text,
    )
    trigger_label = {
        "start": "/start",
        "keyword": f"🔑 {data.get('keyword')}",
        "any": "любое сообщение",
    }.get(data["trigger_type"])
    await message.answer(
        f"✅ Правило добавлено!\n\nТриггер: <b>{trigger_label}</b>",
        parse_mode="HTML",
        reply_markup=back_to_bot(data["bot_id"]),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "view"))
async def cb_ar_view(callback: CallbackQuery, callback_data: AutoReplyCb,
                     pool: asyncpg.Pool) -> None:

    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    r = next((x for x in replies if x["id"] == callback_data.reply_id), None)
    if not r:
        await callback.answer("Правило не найдено.", show_alert=True)
        return
    await callback.answer()
    keyword_escaped = _html.escape(r["keyword"] or "") if r.get("keyword") else ""
    trigger = {
        "start": "/start",
        "keyword": f"🔑 {keyword_escaped}",
        "any": "💬 Любое сообщение",
    }.get(r["trigger_type"])
    status = "✅ Активно" if r["is_active"] else "❌ Отключено"
    response_escaped = _html.escape(r["response_text"] or "")
    await callback.message.edit_text(
        f"<b>Правило #{r['id']}</b>\n\n"
        f"Триггер: {trigger}\n"
        f"Статус: {status}\n\n"
        f"Ответ:\n{response_escaped}",
        parse_mode="HTML",
        reply_markup=auto_reply_view(callback_data.bot_id, r["id"], r["is_active"]),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "toggle"))
async def cb_ar_toggle(callback: CallbackQuery, callback_data: AutoReplyCb,
                       pool: asyncpg.Pool) -> None:

    await callback.answer()
    await db.toggle_auto_reply(pool, callback_data.reply_id, callback_data.bot_id)
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    await callback.message.edit_text(
        f"🤖 <b>Авто-ответы {label}</b>\n\n"
        f"Активных правил: <b>{sum(1 for r in replies if r['is_active'])}</b> из {len(replies)}\n\n"
        "Бот автоматически отвечает на сообщения пользователей по заданным правилам.",
        parse_mode="HTML",
        reply_markup=auto_reply_menu(callback_data.bot_id, replies),
    )
    await callback.answer("✅ Статус изменён.")


@router.callback_query(AutoReplyCb.filter(F.action == "delete"))
async def cb_ar_delete(callback: CallbackQuery, callback_data: AutoReplyCb,
                       pool: asyncpg.Pool) -> None:

    await callback.answer()
    await db.delete_auto_reply(pool, callback_data.reply_id, callback_data.bot_id)
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    await callback.message.edit_text(
        f"🤖 <b>Авто-ответы {label}</b>\n\n"
        f"Активных правил: <b>{sum(1 for r in replies if r['is_active'])}</b> из {len(replies)}\n\n"
        "Бот автоматически отвечает на сообщения пользователей по заданным правилам.",
        parse_mode="HTML",
        reply_markup=auto_reply_menu(callback_data.bot_id, replies),
    )
    await callback.answer("🗑 Правило удалено.")


@router.callback_query(AutoReplyCb.filter(F.action == "copy_to"))
async def cb_ar_copy_to(callback: CallbackQuery, callback_data: AutoReplyCb,
                         pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    bots = await db.get_bots(pool, callback.from_user.id)
    others = [b for b in bots if b["bot_id"] != callback_data.bot_id]
    if not others:
        await callback.answer("Нет других ботов для копирования.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"📋 <b>Копировать авто-ответы из {label}</b>\n\nВыберите бот-получатель:",
        parse_mode="HTML",
        reply_markup=auto_reply_copy_target(callback_data.bot_id, others),
    )


@router.callback_query(AutoReplyCb.filter(F.action == "copy_confirm"))
async def cb_ar_copy_confirm(callback: CallbackQuery, callback_data: AutoReplyCb,
                              pool: asyncpg.Pool) -> None:

    src_bot = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    dst_bot = await db.get_bot(pool, callback_data.target_bot_id, callback.from_user.id)
    if not src_bot or not dst_bot:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    copied = await db.copy_auto_replies(pool, callback_data.bot_id, callback_data.target_bot_id)
    dst_label = f"@{dst_bot['username']}" if dst_bot["username"] else dst_bot["first_name"]
    replies = await db.get_auto_replies(pool, callback_data.bot_id)
    src_label = f"@{src_bot['username']}" if src_bot["username"] else src_bot["first_name"]
    await callback.message.edit_text(
        f"💬 <b>Авто-ответы {src_label}</b>\n\nПравил: {len(replies)}\n\n"
        "Бот автоматически отвечает на сообщения пользователей по заданным правилам.",
        parse_mode="HTML",
        reply_markup=auto_reply_menu(callback_data.bot_id, replies),
    )
    await callback.answer(f"✅ Скопировано {copied} правил в {dst_label}!", show_alert=True)


# ── Extended Automation Rule Handlers (webhook / AI-reply / inactivity) ────────


class AddAutoRuleExt(StatesGroup):
    """FSM for creating automation rules with new action/trigger types."""
    waiting_trigger_value = State()   # inactivity: ask for number of days
    waiting_action_value = State()    # webhook URL or AI system prompt
    waiting_name = State()            # final rule name before saving


TRIGGER_LABELS_EXT = {
    "inactivity": "⏳ Неактивность",
}

ACTION_LABELS_EXT = {
    "webhook": "🔗 Webhook",
    "send_ai_reply": "🤖 AI-ответ",
}


@router.callback_query(AutoCb.filter(F.action == "trig_inactivity"))
async def cb_trig_inactivity(callback: CallbackQuery, callback_data: AutoCb,
                              state: FSMContext) -> None:
    """Trigger: user inactive for N days."""
    await callback.answer()
    await state.update_data(trigger_type="inactivity", bot_id=callback_data.bot_id)
    await state.set_state(AddAutoRuleExt.waiting_trigger_value)
    await callback.message.edit_text(
        "⏳ <b>Триггер: Неактивность</b>\n\n"
        "Введите количество дней неактивности пользователя (например: <code>3</code>):",
        parse_mode="HTML",
        reply_markup=_ar_cancel_kb(callback_data.bot_id),
    )


@router.message(AddAutoRuleExt.waiting_trigger_value, F.text)
async def msg_inactivity_days(message: Message, state: FSMContext) -> None:
    """Receive inactivity days, then ask to choose action."""
    raw = message.text.strip()
    try:
        days = int(raw)
        if days < 1:
            raise ValueError("must be positive")
    except (ValueError, TypeError):
        data = await state.get_data()
        await message.answer("❌ Введите целое положительное число (например: <code>3</code>).",
                             parse_mode="HTML", reply_markup=_ar_cancel_kb(data.get("bot_id", 0)))
        return
    await state.update_data(trigger_value=str(days))
    await state.set_state(AddAutoRuleExt.waiting_action_value)
    data = await state.get_data()
    bot_id = data["bot_id"]
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Отправить сообщение", callback_data=AutoCb(action="ext_act_send", bot_id=bot_id))
    kb.button(text="🔗 Webhook", callback_data=AutoCb(action="ext_act_webhook", bot_id=bot_id))
    kb.button(text="◀️ Назад", callback_data=AutoCb(action="trig_inactivity", bot_id=bot_id))
    kb.button(text="❌ Отмена", callback_data=AutoCb(action="menu", bot_id=bot_id))
    kb.adjust(2, 2)
    await message.answer(
        f"⏳ Неактивность: <b>{days} дн.</b>\n\n"
        "<b>Шаг 2/3</b> — Выберите действие:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoCb.filter(F.action == "act_webhook"))
async def cb_act_webhook(callback: CallbackQuery, callback_data: AutoCb,
                          state: FSMContext) -> None:
    """Action: webhook — ask for URL."""
    await callback.answer()
    await state.update_data(action_type="webhook", bot_id=callback_data.bot_id)
    await state.set_state(AddAutoRuleExt.waiting_action_value)
    await callback.message.edit_text(
        "🔗 <b>Действие: Webhook</b>\n\n"
        "<b>Шаг 3/3</b> — Введите URL для POST-запроса:\n"
        "Пример: <code>https://your-service.com/webhook</code>",
        parse_mode="HTML",
        reply_markup=_ar_cancel_kb(callback_data.bot_id),
    )


@router.callback_query(AutoCb.filter(F.action == "ext_act_webhook"), AddAutoRuleExt.waiting_action_value)
async def cb_ext_act_webhook(callback: CallbackQuery, callback_data: AutoCb,
                              state: FSMContext) -> None:
    """Action: webhook (from inactivity flow) — ask for URL."""
    await callback.answer()
    await state.update_data(action_type="webhook")
    await callback.message.edit_text(
        "🔗 <b>Действие: Webhook</b>\n\n"
        "Введите URL для POST-запроса:\n"
        "Пример: <code>https://your-service.com/webhook</code>",
        parse_mode="HTML",
        reply_markup=_ar_back_cancel_kb(callback_data.bot_id, "trig_inactivity"),
    )


@router.callback_query(AutoCb.filter(F.action == "ext_act_send"), AddAutoRuleExt.waiting_action_value)
async def cb_ext_act_send(callback: CallbackQuery, callback_data: AutoCb,
                           state: FSMContext) -> None:
    """Action: send_message (from inactivity flow) — ask for text."""
    await callback.answer()
    await state.update_data(action_type="send_message")
    await callback.message.edit_text(
        "💬 <b>Действие: Отправить сообщение</b>\n\n"
        "Введите текст сообщения (HTML поддерживается):",
        parse_mode="HTML",
        reply_markup=_ar_back_cancel_kb(callback_data.bot_id, "trig_inactivity"),
    )


@router.callback_query(AutoCb.filter(F.action == "act_ai_reply"))
async def cb_act_ai_reply(callback: CallbackQuery, callback_data: AutoCb,
                           state: FSMContext) -> None:
    """Action: send_ai_reply — ask for system prompt."""
    await callback.answer()
    await state.update_data(action_type="send_ai_reply", bot_id=callback_data.bot_id)
    await state.set_state(AddAutoRuleExt.waiting_action_value)
    await callback.message.edit_text(
        "🤖 <b>Действие: AI-ответ</b>\n\n"
        "<b>Шаг 3/3</b> — Введите системный промпт (описание персонажа/роли AI):\n"
        "Пример: <code>Ты вежливый менеджер по продажам компании X.</code>",
        parse_mode="HTML",
        reply_markup=_ar_cancel_kb(callback_data.bot_id),
    )


@router.message(AddAutoRuleExt.waiting_action_value, F.text)
async def msg_ext_action_value(message: Message, state: FSMContext) -> None:
    """Receive webhook URL or AI system prompt or message text, then ask for rule name."""
    value = message.text.strip()
    if not value:
        data = await state.get_data()
        await message.answer("⚠️ Значение не может быть пустым. Введите снова:", reply_markup=_ar_cancel_kb(data.get("bot_id", 0)))
        return
    await state.update_data(action_value=value)
    await state.set_state(AddAutoRuleExt.waiting_name)
    data = await state.get_data()
    await message.answer(
        "✅ Значение сохранено!\n\nВведите название для этого правила (для вашего удобства):",
        reply_markup=_ar_cancel_kb(data.get("bot_id", 0)),
    )


@router.message(AddAutoRuleExt.waiting_name, F.text)
async def msg_ext_rule_name(message: Message, state: FSMContext,
                             pool: asyncpg.Pool) -> None:
    """Save the new automation rule with extended action/trigger types."""
    rule_name = message.text.strip()
    if not rule_name:
        data = await state.get_data()
        await message.answer("⚠️ Название не может быть пустым. Введите снова:", reply_markup=_ar_cancel_kb(data.get("bot_id", 0)))
        return
    data = await state.get_data()
    await state.clear()
    bot_id = data["bot_id"]
    trigger_type = data["trigger_type"]
    trigger_value = data.get("trigger_value")
    action_type = data["action_type"]
    action_value = data.get("action_value", "")

    await db.add_automation_rule(
        pool, bot_id, rule_name,
        trigger_type, trigger_value,
        action_type, action_value,
    )

    trigger_label = TRIGGER_LABELS_EXT.get(trigger_type, trigger_type)
    action_label = ACTION_LABELS_EXT.get(action_type, action_type)

    # Build a human-readable value summary
    if action_type == "webhook":
        value_summary = f"URL: <code>{action_value[:80]}</code>"
    elif action_type == "send_ai_reply":
        value_summary = f"Промпт: <code>{action_value[:80]}</code>"
    else:
        value_summary = f"<code>{action_value[:80]}</code>"

    await message.answer(
        f"✅ <b>Правило создано!</b>\n\n"
        f"Название: {rule_name}\n"
        f"Триггер: {trigger_label}\n"
        f"Действие: {action_label}\n"
        f"{value_summary}",
        parse_mode="HTML",
        reply_markup=back_to_bot(bot_id),
    )

"""Message templates: save, list, use for broadcasts, delete, AI generation."""

import html as _html
import logging
import os
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg
from bot.callbacks import TemplateCb, BmCb
from bot.keyboards import (
    templates_list,
    template_actions,
    broadcast_confirm,
    back_to_bot,
    subscription_locked_markup,
)
from bot.states import AddTemplate, Broadcast, AiTemplateGenFSM, EditTemplate
from bot.utils.template_validator import (
    validate_message_template,
    list_placeholders,
    replace_placeholders,
)
from database import db
from bot.utils.subscription import locked_text, require_plan

router = Router()
log = logging.getLogger(__name__)

_AI_SYSTEM = (
    "Ты помощник по написанию сообщений для Telegram-ботов. "
    "Пиши ТОЛЬКО текст шаблона — без заголовков, пояснений, кавычек вокруг ответа. "
    "Используй HTML-теги для форматирования: <b>жирный</b>, <i>курсив</i>. "
    "Плейсхолдеры пиши в формате {{NAME}}. "
    "Длина — до 1000 символов. Тон — согласно запросу пользователя."
)


def _ai_preview_kb(bot_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💾 Сохранить шаблон",
        callback_data=TemplateCb(action="ai_save", bot_id=bot_id),
    )
    kb.button(
        text="🔄 Перегенерировать",
        callback_data=TemplateCb(action="ai_regen", bot_id=bot_id),
    )
    kb.button(text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id))
    kb.adjust(1)
    return kb.as_markup()


async def _call_ai(prompt: str) -> str | None:
    from services.ai_providers import configured_providers

    providers = configured_providers()
    if not providers:
        return None
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return None
    for provider in providers:
        model = provider.models[0] if provider.models else ""
        if not model:
            continue
        try:
            client = AsyncOpenAI(api_key=provider.api_key, base_url=provider.base_url, timeout=25.0)
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _AI_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=800,
                temperature=0.7,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as exc:
            log.warning("AI template gen failed provider=%s: %s", provider.name, exc)
    return None


@router.callback_query(TemplateCb.filter(F.action == "list"))
async def cb_templates_list(
    callback: CallbackQuery, callback_data: TemplateCb, pool: asyncpg.Pool
) -> None:

    await callback.answer()
    bot_id = callback_data.bot_id
    templates = await db.get_templates(pool, callback.from_user.id)
    count = len(templates)
    header = (
        f"📝 <b>Шаблоны сообщений</b>\n\nВсего: {count}"
        if count
        else "📝 <b>Шаблоны сообщений</b>\n\nШаблонов ещё нет."
    )
    hint = (
        "\n\n📌 <b>Что это?</b>\n"
        "Шаблоны — готовые тексты сообщений, которые можно быстро вставлять при рассылке.\n\n"
        "💡 <b>Как использовать:</b>\n"
        "• Создайте шаблон один раз\n"
        "• Используйте его в рассылках без повторного ввода\n"
        "• Шаблоны доступны для всех ботов в аккаунте"
    )
    await callback.message.edit_text(
        header + hint,
        parse_mode="HTML",
        reply_markup=templates_list(templates, bot_id),
    )


@router.callback_query(TemplateCb.filter(F.action == "add"))
async def cb_template_add(
    callback: CallbackQuery, callback_data: TemplateCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AddTemplate.waiting_name)
    bot_id = callback_data.bot_id
    await state.update_data(bot_id=bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id))
    await callback.message.edit_text(
        "📝 <b>Новый шаблон — шаг 1/2</b>\n\n"
        "Введите название шаблона (например: <i>Акция</i>, <i>Приветствие</i>):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AddTemplate.waiting_name, F.text)
async def msg_template_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    _bot_id = data.get("bot_id", 0)
    name = message.text.strip() if message.text else ""
    if not name:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=_bot_id)
        )
        await message.answer(
            "❌ Название не может быть пустым. Введите ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return
    if len(name) > 64:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=_bot_id)
        )
        await message.answer(
            "❌ Название слишком длинное (макс. 64 символа). Введите ещё раз:",
            reply_markup=kb.as_markup(),
        )
        return
    await state.update_data(name=name)
    await state.set_state(AddTemplate.waiting_text)
    data = await state.get_data()
    _bot_id = data.get("bot_id", 0)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=_bot_id))
    await message.answer(
        "📝 <b>Новый шаблон — шаг 2/2</b>\n\n"
        "Напишите текст сообщения.\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
        "<code>&lt;i&gt;курсив&lt;/i&gt;</code>, "
        "<code>&lt;a href=...&gt;ссылка&lt;/a&gt;</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AddTemplate.waiting_text, F.text)
async def msg_template_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    name = data["name"]
    text = message.text or message.caption or ""
    bot_id = data.get("bot_id", 0)

    # Validate before saving
    validation = validate_message_template(name, text)
    if not validation.valid:
        errors = "\n".join(f"• {e}" for e in validation.errors)
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id)
        )
        await message.answer(
            f"❌ <b>Ошибки в шаблоне:</b>\n{errors}\n\nИсправьте и отправьте снова.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    if validation.warnings:
        warns = "\n".join(f"⚠️ {w}" for w in validation.warnings)
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id)
        )
        await message.answer(
            f"<b>Предупреждения:</b>\n{warns}\n\nВсё равно сохранить? Напишите текст снова для подтверждения.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    await state.clear()

    saved = await db.save_template(pool, message.from_user.id, name, text)
    if saved:
        await message.answer(
            f"✅ Шаблон <b>«{_html.escape(name)}»</b> сохранён!",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id) if bot_id else None,
        )
    else:
        await message.answer(
            f"⚠️ Шаблон с именем <b>«{_html.escape(name)}»</b> уже существует. Выберите другое название.",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id) if bot_id else None,
        )


@router.callback_query(TemplateCb.filter(F.action == "view"))
async def cb_template_view(
    callback: CallbackQuery, callback_data: TemplateCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    tpl = await db.get_template(pool, callback_data.template_id, callback.from_user.id)
    if not tpl:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад",
            callback_data=TemplateCb(action="list", bot_id=callback_data.bot_id),
        )
        await callback.message.edit_text(
            "❌ Шаблон не найден.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    preview = tpl["text"][:900] + ("…" if len(tpl["text"]) > 900 else "")
    await callback.message.edit_text(
        f"📝 <b>{_html.escape(tpl['name'])}</b>\n\n{preview}",
        parse_mode="HTML",
        reply_markup=template_actions(callback_data.template_id, callback_data.bot_id),
    )


@router.callback_query(TemplateCb.filter(F.action == "delete"))
async def cb_template_delete(
    callback: CallbackQuery, callback_data: TemplateCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    deleted = await db.delete_template(
        pool, callback_data.template_id, callback.from_user.id
    )
    if not deleted:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад",
            callback_data=TemplateCb(action="list", bot_id=callback_data.bot_id),
        )
        await callback.message.edit_text(
            "❌ Не удалось удалить шаблон.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    templates = await db.get_templates(pool, callback.from_user.id)
    bot_id = callback_data.bot_id
    count = len(templates)
    header = (
        f"✅ Шаблон удалён. Осталось: {count}"
        if count
        else "✅ Шаблон удалён. Шаблонов больше нет."
    )
    await callback.message.edit_text(
        f"📝 <b>Шаблоны</b>\n\n{header}",
        parse_mode="HTML",
        reply_markup=templates_list(templates, bot_id),
    )


@router.callback_query(TemplateCb.filter(F.action == "use"))
async def cb_template_use(
    callback: CallbackQuery,
    callback_data: TemplateCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    tpl = await db.get_template(pool, callback_data.template_id, callback.from_user.id)
    if not tpl:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад",
            callback_data=TemplateCb(action="list", bot_id=callback_data.bot_id),
        )
        await callback.message.edit_text(
            "❌ Шаблон не найден.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    bot_id = callback_data.bot_id
    template_text = tpl["text"]

    # Detect placeholders
    placeholders = list_placeholders(template_text)
    if placeholders:
        await state.set_state(Broadcast.waiting_placeholders)
        await state.update_data(
            bot_id=bot_id,
            template_id=callback_data.template_id,
            template_text=template_text,
            placeholders=placeholders,
        )
        ph_list = "\n".join(f"  • <code>{{{{{p}}}}}</code>" for p in placeholders)
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id)
        )
        await callback.message.edit_text(
            f"📢 <b>Рассылка по шаблону «{_html.escape(tpl['name'])}»</b>\n\n"
            f"В шаблоне найдены плейсхолдеры:\n{ph_list}\n\n"
            "Отправьте значения в формате:\n"
            "<code>ключ=значение, ключ=значение</code>\n\n"
            "Пример:\n"
            "<code>NAME=Иван, CITY=Москва</code>\n\n"
            "✏️ Напишите в поле сообщения ниже ↓",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    # No placeholders — go directly to confirm
    count = await db.get_audience_count(pool, bot_id)
    preview = template_text[:600] + ("…" if len(template_text) > 600 else "")

    await state.set_state(Broadcast.confirming)
    await state.update_data(bot_id=bot_id, text=template_text)

    await callback.message.edit_text(
        f"📢 <b>Рассылка по шаблону «{_html.escape(tpl['name'])}»</b>\n\n"
        f"{preview}\n\n"
        f"Получателей: <b>{count}</b> чел.\n\n"
        "Запустить рассылку?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(bot_id),
    )


@router.message(Broadcast.waiting_placeholders, F.text)
async def msg_placeholders(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Parse placeholder values, render template, proceed to broadcast confirm."""
    data = await state.get_data()
    placeholders: list = data.get("placeholders", [])
    template_text: str = data.get("template_text", "")
    bot_id: int = data.get("bot_id", 0)

    # Parse: key=value, key=value
    raw = (message.text or "").strip()
    variables: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            key, val = part.split("=", 1)
            variables[key.strip()] = val.strip()

    # Check all placeholders are filled
    missing = [p for p in placeholders if p not in variables]
    if missing:
        ph_list = "\n".join(f"• <code>{{{{{p}}}}}</code>" for p in missing)
        await message.answer(
            f"❌ Не все плейсхолдеры заполнены:\n{ph_list}\n\n"
            "Отправьте значения ещё раз:",
            parse_mode="HTML",
        )
        return

    # Render
    rendered = replace_placeholders(template_text, variables)

    # Show preview
    count = await db.get_audience_count(pool, bot_id)
    preview = rendered[:600] + ("…" if len(rendered) > 600 else "")
    unfilled = list_placeholders(rendered)
    extra = ""
    if unfilled:
        extra = "\n\n⚠️ Остались незаполненные: " + ", ".join(
            f"<code>{{{{{p}}}}}</code>" for p in unfilled
        )

    await state.set_state(Broadcast.confirming)
    await state.update_data(text=rendered, bot_id=bot_id)

    await message.answer(
        f"📢 <b>Рассылка — проверка</b>\n\n"
        f"{preview}{extra}\n\n"
        f"Получателей: <b>{count}</b> чел.\n\n"
        "Запустить рассылку?",
        parse_mode="HTML",
        reply_markup=broadcast_confirm(bot_id),
    )


# ── AI template generation ────────────────────────────────────────────────────


@router.callback_query(TemplateCb.filter(F.action == "ai_gen"))
async def cb_template_ai_gen(
    callback: CallbackQuery,
    callback_data: TemplateCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.message.edit_text(
            locked_text("AI-генерация шаблонов", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="assets")),
        )
        return
    bot_id = callback_data.bot_id
    await state.set_state(AiTemplateGenFSM.waiting_prompt)
    await state.update_data(bot_id=bot_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id))
    await callback.message.edit_text(
        "✨ <b>AI-генерация шаблона</b>\n\n"
        "Опишите, что должно быть в шаблоне.\n\n"
        "<b>Примеры:</b>\n"
        "• <i>Приветствие для нового подписчика, дружелюбный тон</i>\n"
        "• <i>Уведомление об акции -30%, призыв купить сегодня</i>\n"
        "• <i>Напоминание о вебинаре завтра в 19:00, тема: продажи</i>\n\n"
        "Просто опишите своими словами — AI напишет готовый текст.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AiTemplateGenFSM.waiting_prompt, F.text)
async def msg_ai_template_prompt(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    prompt = (message.text or "").strip()
    if not prompt:
        await message.answer("❌ Описание не может быть пустым. Попробуйте снова:")
        return
    if not await require_plan(pool, message.from_user.id, "starter"):
        await state.clear()
        await message.answer(
            locked_text("AI-генерация шаблонов", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="assets")),
        )
        return

    data = await state.get_data()
    bot_id = data.get("bot_id", 0)

    wait_msg = await message.answer("⏳ Генерирую текст…")

    generated = await _call_ai(f"Создай шаблон сообщения для Telegram-бота: {prompt}")

    if not generated:
        await wait_msg.delete()
        await message.answer(
            "⚠️ <b>AI недоступен.</b>\n"
            "Проверьте переменную <code>OPENROUTER_API_KEY</code> в настройках.",
            parse_mode="HTML",
            reply_markup=_ai_preview_kb(bot_id),
        )
        await state.clear()
        return

    await state.update_data(generated_text=generated, prompt=prompt)
    await state.set_state(AiTemplateGenFSM.waiting_name)

    preview = generated[:900] + ("…" if len(generated) > 900 else "")
    await wait_msg.delete()
    await message.answer(
        f"✨ <b>Готово! Вот что получилось:</b>\n\n"
        f"{preview}\n\n"
        "Нажмите <b>«💾 Сохранить шаблон»</b>, чтобы задать название и сохранить.\n"
        "Или <b>«🔄 Перегенерировать»</b> для нового варианта.",
        parse_mode="HTML",
        reply_markup=_ai_preview_kb(bot_id),
    )


@router.callback_query(TemplateCb.filter(F.action == "ai_regen"))
async def cb_template_ai_regen(
    callback: CallbackQuery,
    callback_data: TemplateCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer("⏳ Генерирую…")
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await state.clear()
        await callback.message.edit_text(
            locked_text("AI-генерация шаблонов", "starter"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("starter", back_callback=BmCb(action="assets")),
        )
        return
    data = await state.get_data()
    prompt = data.get("prompt", "")
    bot_id = callback_data.bot_id

    if not prompt:
        await state.set_state(AiTemplateGenFSM.waiting_prompt)
        await state.update_data(bot_id=bot_id)
        kb = InlineKeyboardBuilder()
        kb.button(
            text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id)
        )
        await callback.message.edit_text(
            "✨ <b>AI-генерация шаблона</b>\n\nОпишите шаблон заново:",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    generated = await _call_ai(f"Создай шаблон сообщения для Telegram-бота: {prompt}")

    if not generated:
        await callback.message.edit_text(
            "⚠️ <b>AI недоступен.</b> Попробуйте позже.",
            parse_mode="HTML",
            reply_markup=_ai_preview_kb(bot_id),
        )
        return

    await state.update_data(generated_text=generated)
    await state.set_state(AiTemplateGenFSM.waiting_name)

    preview = generated[:900] + ("…" if len(generated) > 900 else "")
    await callback.message.edit_text(
        f"✨ <b>Новый вариант:</b>\n\n"
        f"{preview}\n\n"
        "Нажмите <b>«💾 Сохранить шаблон»</b> или <b>«🔄 Перегенерировать»</b>.",
        parse_mode="HTML",
        reply_markup=_ai_preview_kb(bot_id),
    )


@router.callback_query(TemplateCb.filter(F.action == "ai_save"))
async def cb_template_ai_save(
    callback: CallbackQuery, callback_data: TemplateCb, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    generated_text = data.get("generated_text", "")
    bot_id = callback_data.bot_id

    if not generated_text:
        await state.clear()
        kb = InlineKeyboardBuilder()
        kb.button(
            text="◀️ Назад", callback_data=TemplateCb(action="list", bot_id=bot_id)
        )
        await callback.message.edit_text(
            "❌ Нет сгенерированного текста. Попробуйте сгенерировать заново.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    await state.set_state(AiTemplateGenFSM.waiting_name)
    await state.update_data(bot_id=bot_id, generated_text=generated_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=TemplateCb(action="list", bot_id=bot_id))
    await callback.message.edit_text(
        "💾 <b>Сохранение шаблона</b>\n\nВведите название для этого шаблона:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(TemplateCb.filter(F.action == "edit"))
async def cb_template_edit(
    callback: CallbackQuery, callback_data: TemplateCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    tpl = await db.get_template(pool, callback_data.template_id, callback.from_user.id)
    if not tpl:
        await callback.answer("❌ Шаблон не найден", show_alert=True)
        return
    await callback.answer()
    await state.set_state(EditTemplate.waiting_name)
    await state.update_data(
        template_id=callback_data.template_id,
        bot_id=callback_data.bot_id,
        old_name=tpl["name"],
        old_text=tpl["text"],
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="⏩ Оставить текущее", callback_data=TemplateCb(action="edit_keep_name", template_id=callback_data.template_id, bot_id=callback_data.bot_id))
    kb.button(text="❌ Отмена", callback_data=TemplateCb(action="view", template_id=callback_data.template_id, bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✏️ <b>Редактирование шаблона «{_html.escape(tpl['name'])}»</b>\n\n"
        "Введите новое название (или нажмите «Оставить текущее»):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(TemplateCb.filter(F.action == "edit_keep_name"))
async def cb_template_edit_keep_name(
    callback: CallbackQuery, callback_data: TemplateCb, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.set_state(EditTemplate.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏩ Оставить текущий текст", callback_data=TemplateCb(action="edit_keep_text", template_id=callback_data.template_id, bot_id=callback_data.bot_id))
    kb.button(text="❌ Отмена", callback_data=TemplateCb(action="view", template_id=callback_data.template_id, bot_id=callback_data.bot_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✏️ Название оставлено: <b>{_html.escape(data.get('old_name', ''))}</b>\n\n"
        "Введите новый текст шаблона:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(TemplateCb.filter(F.action == "edit_keep_text"))
async def cb_template_edit_keep_text(
    callback: CallbackQuery, callback_data: TemplateCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()
    ok = await db.update_template(
        pool,
        data["template_id"],
        callback.from_user.id,
        data.get("new_name") or data["old_name"],
        data["old_text"],
    )
    if ok:
        await callback.message.edit_text(
            f"✅ Шаблон обновлён.",
            parse_mode="HTML",
            reply_markup=template_actions(data["template_id"], data.get("bot_id", 0)),
        )
    else:
        await callback.answer("❌ Не удалось обновить шаблон", show_alert=True)


@router.message(EditTemplate.waiting_name, F.text)
async def msg_edit_template_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("❌ Название от 1 до 64 символов:")
        return
    await state.update_data(new_name=name)
    await state.set_state(EditTemplate.waiting_text)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏩ Оставить текущий текст", callback_data="tpl_edit_keep_text_fsm")
    kb.adjust(1)
    data = await state.get_data()
    await message.answer(
        f"✅ Новое название: <b>{_html.escape(name)}</b>\n\n"
        "Введите новый текст шаблона или нажмите «Оставить текущий текст»:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(lambda c: c.data == "tpl_edit_keep_text_fsm")
async def cb_edit_keep_text_fsm(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    data = await state.get_data()
    if not data.get("template_id"):
        await callback.answer("Сессия истекла. Начните заново.", show_alert=True)
        return
    await state.clear()
    ok = await db.update_template(
        pool,
        data["template_id"],
        callback.from_user.id,
        data.get("new_name") or data["old_name"],
        data["old_text"],
    )
    if ok:
        await callback.answer()
        await callback.message.edit_text(
            "✅ Шаблон обновлён.",
            parse_mode="HTML",
            reply_markup=template_actions(data["template_id"], data.get("bot_id", 0)),
        )
    else:
        await callback.answer("❌ Не удалось обновить шаблон", show_alert=True)


@router.message(EditTemplate.waiting_text, F.text)
async def msg_edit_template_text(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Текст не может быть пустым:")
        return
    data = await state.get_data()
    await state.clear()
    ok = await db.update_template(
        pool,
        data["template_id"],
        message.from_user.id,
        data.get("new_name") or data["old_name"],
        text,
    )
    if ok:
        await message.answer(
            "✅ Шаблон обновлён.",
            parse_mode="HTML",
            reply_markup=template_actions(data["template_id"], data.get("bot_id", 0)),
        )
    else:
        await message.answer("❌ Шаблон с таким именем уже существует.")
        await state.set_state(EditTemplate.waiting_name)
        await state.update_data(**data)


@router.message(AiTemplateGenFSM.waiting_name, F.text)
async def msg_ai_template_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым:")
        return
    if len(name) > 64:
        await message.answer("❌ Название слишком длинное (макс. 64 символа):")
        return

    data = await state.get_data()
    generated_text = data.get("generated_text", "")
    bot_id = data.get("bot_id", 0)
    await state.clear()

    saved = await db.save_template(pool, message.from_user.id, name, generated_text)
    if saved:
        await message.answer(
            f"✅ Шаблон <b>«{_html.escape(name)}»</b> сохранён!\n\n"
            "Его можно найти в разделе <b>Шаблоны</b> и использовать для рассылки.",
            parse_mode="HTML",
            reply_markup=back_to_bot(bot_id) if bot_id else None,
        )
    else:
        await message.answer(
            f"⚠️ Шаблон с именем <b>«{_html.escape(name)}»</b> уже существует. Введите другое:",
            parse_mode="HTML",
        )
        await state.set_state(AiTemplateGenFSM.waiting_name)
        await state.update_data(bot_id=bot_id, generated_text=generated_text)

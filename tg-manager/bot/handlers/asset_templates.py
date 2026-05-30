"""Asset Templates handler.

Manages reusable templates for Telegram assets:
  - Bot templates (name, description, short_description)
  - Channel templates (title, description, username)
  - Group templates (title, description, username)
  - Post templates (text with optional HTML markup)

Callback prefix: atpl
"""
from __future__ import annotations

import html
import json
import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
import asyncpg

from bot.callbacks import AssetTplCb, ChanFactCb, GroupFCb, MassPubCb, MassOpCb, LibCb, TplBotApplyCb
from bot.states import AssetTemplateFSM, ChannelFactoryFSM, CreateGroupFSM, BulkJoinFSM, BulkLeaveFSM
from bot.utils.op_helpers import _get_active_accounts
from bot.utils.template_validator import validate_asset_template
from database import db

log = logging.getLogger(__name__)
router = Router()

# ── Asset type metadata ────────────────────────────────────────────────────────

_TYPE_LABELS = {
    "bot":       "🤖 Бот",
    "channel":   "📡 Канал",
    "group":     "👥 Группа",
    "post":      "📝 Пост",
    "operation": "⚙️ Операция",
}

_TYPE_PROMPTS = {
    "bot": (
        "🤖 <b>Шаблон бота — параметры</b>\n\n"
        "Введите имя бота, описание и короткое описание через <code>;;;</code>\n\n"
        "Пример:\n"
        "<code>Мой магазин;;;Магазин одежды;;;Купить одежду</code>"
    ),
    "channel": (
        "📡 <b>Шаблон канала — параметры</b>\n\n"
        "Введите через <code>;;;</code>: название канала, описание, username "
        "(или оставьте пустым).\n\n"
        "Пример:\n"
        "<code>Мой канал;;;Новости о моде;;;fashion_shop</code>"
    ),
    "group": (
        "👥 <b>Шаблон группы — параметры</b>\n\n"
        "Введите через <code>;;;</code>: название группы, описание, username "
        "(или оставьте пустым).\n\n"
        "Пример:\n"
        "<code>Моя группа;;;Обсуждения о моде;;;fashion_chat</code>"
    ),
    "post": (
        "📝 <b>Шаблон поста — параметры</b>\n\n"
        "Введите текст поста (поддерживается HTML-разметка: "
        "<code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
        "<code>&lt;a href=...&gt;</code> и т.д.)."
    ),
    "operation": (
        "⚙️ <b>Шаблон операции — параметры</b>\n\n"
        "Формат: <code>тип;;;параметр</code>\n\n"
        "Типы и примеры:\n"
        "• <code>mass_publish;;;Текст поста</code>\n"
        "• <code>bulk_join;;;@channel1\n@channel2</code>\n"
        "• <code>bulk_leave;;;@channel1\n@channel2</code>\n"
        "• <code>bulk_bot_edit;;;name;;;Новое имя</code>\n\n"
        "Для bulk_bot_edit поле: <code>name</code>, <code>desc</code>, <code>short_desc</code>"
    ),
}


# ── Keyboard helpers ───────────────────────────────────────────────────────────

def _menu_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="📚 Библиотека готовых",  callback_data=LibCb(action="menu"))
    kb.button(text="🤖 Мои боты",            callback_data=AssetTplCb(action="list", asset_type="bot"))
    kb.button(text="📡 Мои каналы",          callback_data=AssetTplCb(action="list", asset_type="channel"))
    kb.button(text="👥 Мои группы",          callback_data=AssetTplCb(action="list", asset_type="group"))
    kb.button(text="📝 Мои посты",           callback_data=AssetTplCb(action="list", asset_type="post"))
    kb.button(text="⚙️ Мои операции",        callback_data=AssetTplCb(action="list", asset_type="operation"))
    kb.button(text="➕ Создать свой",         callback_data=AssetTplCb(action="create"))
    kb.button(text="◀️ Назад",               callback_data=AssetTplCb(action="back"))
    kb.adjust(1, 2, 2, 2, 1)
    return kb.as_markup()


def _list_kb(templates: list, asset_type: str) -> object:
    kb = InlineKeyboardBuilder()
    for tpl in templates:
        kb.button(
            text=f"📄 {tpl['name']}",
            callback_data=AssetTplCb(action="view", tpl_id=tpl["id"], asset_type=asset_type),
        )
        kb.button(
            text="👁️ Просмотр",
            callback_data=AssetTplCb(action="view", tpl_id=tpl["id"], asset_type=asset_type),
        )
        kb.button(
            text="🗑️ Удалить",
            callback_data=AssetTplCb(action="delete_confirm", tpl_id=tpl["id"], asset_type=asset_type),
        )
    kb.button(text="◀️ Назад", callback_data=AssetTplCb(action="menu"))
    kb.adjust(1, *([3] * len(templates)), 1)
    return kb.as_markup()


def _type_choice_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Бот",         callback_data=AssetTplCb(action="choose_type", asset_type="bot"))
    kb.button(text="📡 Канал",       callback_data=AssetTplCb(action="choose_type", asset_type="channel"))
    kb.button(text="👥 Группа",      callback_data=AssetTplCb(action="choose_type", asset_type="group"))
    kb.button(text="📝 Пост",        callback_data=AssetTplCb(action="choose_type", asset_type="post"))
    kb.button(text="⚙️ Операция",   callback_data=AssetTplCb(action="choose_type", asset_type="operation"))
    kb.button(text="◀️ Отмена",     callback_data=AssetTplCb(action="menu"))
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _confirm_kb(asset_type: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сохранить", callback_data=AssetTplCb(action="save", asset_type=asset_type))
    kb.button(text="❌ Отмена",    callback_data=AssetTplCb(action="menu"))
    kb.adjust(2)
    return kb.as_markup()


def _delete_confirm_kb(tpl_id: int, asset_type: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Да, удалить",
        callback_data=AssetTplCb(action="delete", tpl_id=tpl_id, asset_type=asset_type),
    )
    kb.button(
        text="◀️ Отмена",
        callback_data=AssetTplCb(action="list", asset_type=asset_type),
    )
    kb.adjust(2)
    return kb.as_markup()


def _view_kb(tpl_id: int, asset_type: str) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🚀 Применить",
        callback_data=AssetTplCb(action="apply", tpl_id=tpl_id, asset_type=asset_type),
    )
    kb.button(
        text="🗑️ Удалить",
        callback_data=AssetTplCb(action="delete_confirm", tpl_id=tpl_id, asset_type=asset_type),
    )
    kb.button(
        text="◀️ Назад",
        callback_data=AssetTplCb(action="list", asset_type=asset_type),
    )
    kb.adjust(2, 1)
    return kb.as_markup()


# ── DB helpers ─────────────────────────────────────────────────────────────────

async def _get_templates(pool: asyncpg.Pool, owner_id: int, asset_type: str) -> list:
    return await pool.fetch(
        """SELECT id, name, template, created_at FROM asset_templates
           WHERE owner_id=$1 AND asset_type=$2
           ORDER BY created_at DESC LIMIT 10""",
        owner_id, asset_type,
    )


async def _get_template(pool: asyncpg.Pool, tpl_id: int, owner_id: int):
    return await pool.fetchrow(
        "SELECT * FROM asset_templates WHERE id=$1 AND owner_id=$2",
        tpl_id, owner_id,
    )


async def _save_template(
    pool: asyncpg.Pool,
    owner_id: int,
    asset_type: str,
    name: str,
    template: dict,
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO asset_templates (owner_id, asset_type, name, template)
           VALUES ($1, $2, $3, $4)
           RETURNING id""",
        owner_id, asset_type, name, json.dumps(template),
    )
    return row["id"]


async def _delete_template(pool: asyncpg.Pool, tpl_id: int, owner_id: int) -> bool:
    result = await pool.execute(
        "DELETE FROM asset_templates WHERE id=$1 AND owner_id=$2",
        tpl_id, owner_id,
    )
    return result != "DELETE 0"


# ── Handlers ───────────────────────────────────────────────────────────────────

@router.callback_query(AssetTplCb.filter(F.action == "menu"))
async def cb_menu(callback: CallbackQuery, callback_data: AssetTplCb) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "📄 <b>Шаблоны ассетов</b>\n\n"
        "Здесь вы можете создавать и управлять шаблонами для быстрого создания "
        "ботов, каналов, групп и постов.",
        parse_mode="HTML",
        reply_markup=_menu_kb(),
    )


@router.callback_query(AssetTplCb.filter(F.action == "list"))
async def cb_list(
    callback: CallbackQuery,
    callback_data: AssetTplCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    asset_type = callback_data.asset_type or ""
    label = _TYPE_LABELS.get(asset_type, asset_type)
    templates = await _get_templates(pool, callback.from_user.id, asset_type)

    if templates:
        text = f"📄 <b>Шаблоны: {label}</b>\n\nНайдено: {len(templates)} шт."
    else:
        text = (
            f"📄 <b>Шаблоны: {label}</b>\n\n"
            "Шаблонов пока нет. Нажмите <b>➕ Создать</b> в главном меню."
        )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_list_kb(templates, asset_type),
    )


@router.callback_query(AssetTplCb.filter(F.action == "view"))
async def cb_view(
    callback: CallbackQuery,
    callback_data: AssetTplCb,
    pool: asyncpg.Pool,
) -> None:
    tpl = await _get_template(pool, callback_data.tpl_id, callback.from_user.id)
    if not tpl:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await callback.answer()

    try:
        data = json.loads(tpl["template"]) if isinstance(tpl["template"], str) else tpl["template"]
    except Exception:
        data = {}

    lines = [f"📄 <b>{tpl['name']}</b>", f"Тип: {_TYPE_LABELS.get(tpl['asset_type'], tpl['asset_type'])}"]
    for k, v in data.items():
        lines.append(f"<b>{k}:</b> {v}")

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_view_kb(callback_data.tpl_id, callback_data.asset_type or ""),
    )


# ── Create wizard ──────────────────────────────────────────────────────────────

@router.callback_query(AssetTplCb.filter(F.action == "create"))
async def cb_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AssetTemplateFSM.choosing_type)
    await callback.message.edit_text(
        "➕ <b>Создание шаблона</b>\n\nВыберите тип ассета:",
        parse_mode="HTML",
        reply_markup=_type_choice_kb(),
    )


@router.callback_query(AssetTplCb.filter(F.action == "choose_type"), AssetTemplateFSM.choosing_type)
async def cb_choose_type(callback: CallbackQuery, callback_data: AssetTplCb, state: FSMContext) -> None:
    await callback.answer()
    asset_type = callback_data.asset_type or ""
    await state.update_data(asset_type=asset_type)
    await state.set_state(AssetTemplateFSM.waiting_name)
    label = _TYPE_LABELS.get(asset_type, asset_type)
    await callback.message.edit_text(
        f"➕ <b>Шаблон {label} — шаг 1/2</b>\n\nВведите название шаблона:",
        parse_mode="HTML",
    )


@router.message(AssetTemplateFSM.waiting_name, F.text)
async def msg_waiting_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ Название не может быть пустым. Попробуйте ещё раз:")
        return
    if len(name) > 64:
        await message.answer("❌ Название слишком длинное (максимум 64 символа). Попробуйте ещё раз:")
        return

    await state.update_data(name=name)
    await state.set_state(AssetTemplateFSM.waiting_json)

    data = await state.get_data()
    asset_type = data.get("asset_type", "bot")
    prompt = _TYPE_PROMPTS.get(asset_type, "Введите параметры шаблона:")
    label = _TYPE_LABELS.get(asset_type, asset_type)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AssetTplCb(action="menu"))
    await message.answer(
        f"➕ <b>Шаблон {label} — шаг 2/2</b>\n\n{prompt}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AssetTemplateFSM.waiting_json, F.text)
async def msg_waiting_json(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    data = await state.get_data()
    asset_type = data.get("asset_type", "bot")
    name = data.get("name", "")

    # Parse input into a template dict
    if asset_type == "post":
        template = {"text": raw}
    elif asset_type == "operation":
        parts = [p.strip() for p in raw.split(";;;")]
        op_type = parts[0] if parts else "mass_publish"
        if op_type == "mass_publish":
            template = {"op_type": op_type, "text": parts[1] if len(parts) > 1 else ""}
        elif op_type == "bulk_join":
            links = [ln.strip() for ln in (parts[1] if len(parts) > 1 else "").splitlines() if ln.strip()]
            template = {"op_type": op_type, "links": links}
        elif op_type == "bulk_leave":
            channels = [ln.strip() for ln in (parts[1] if len(parts) > 1 else "").splitlines() if ln.strip()]
            template = {"op_type": op_type, "channels": channels}
        elif op_type == "bulk_bot_edit":
            template = {
                "op_type": op_type,
                "field": parts[1] if len(parts) > 1 else "",
                "value": parts[2] if len(parts) > 2 else "",
            }
        else:
            template = {"op_type": op_type}
        if not op_type or op_type not in ("mass_publish", "bulk_join", "bulk_leave", "bulk_bot_edit"):
            await message.answer(
                "❌ Неверный тип операции. Используйте: "
                "<code>mass_publish</code>, <code>bulk_join</code>, "
                "<code>bulk_leave</code>, <code>bulk_bot_edit</code>",
                parse_mode="HTML",
            )
            return
    else:
        parts = [p.strip() for p in raw.split(";;;")]
        if asset_type == "bot":
            template = {
                "name":              parts[0] if len(parts) > 0 else "",
                "description":       parts[1] if len(parts) > 1 else "",
                "short_description": parts[2] if len(parts) > 2 else "",
            }
        else:  # channel or group
            template = {
                "title":       parts[0] if len(parts) > 0 else "",
                "description": parts[1] if len(parts) > 1 else "",
                "username":    parts[2] if len(parts) > 2 else "",
            }

    # Validate the parsed template
    validation = validate_asset_template(asset_type, name, template)
    if not validation.valid:
        errors = "\n".join(f"• {e}" for e in validation.errors)
        await message.answer(
            f"❌ <b>Ошибки в шаблоне:</b>\n{errors}\n\nВведите параметры заново:",
            parse_mode="HTML",
        )
        return

    await state.update_data(template=template)
    await state.set_state(AssetTemplateFSM.confirming)

    label = _TYPE_LABELS.get(asset_type, asset_type)
    lines = [f"✅ <b>Проверьте шаблон</b>", f"Тип: {label}", f"Название: <b>{name}</b>"]
    for k, v in template.items():
        if v:
            lines.append(f"<b>{k}:</b> {v}")

    if validation.warnings:
        lines.append("\n⚠️ <b>Замечания:</b>")
        lines.extend(f"• {w}" for w in validation.warnings)

    await message.answer(
        "\n".join(lines) + "\n\nСохранить шаблон?",
        parse_mode="HTML",
        reply_markup=_confirm_kb(asset_type),
    )


@router.callback_query(AssetTplCb.filter(F.action == "save"), AssetTemplateFSM.confirming)
async def cb_save(
    callback: CallbackQuery,
    callback_data: AssetTplCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    data = await state.get_data()
    asset_type = data.get("asset_type", callback_data.asset_type or "")
    name = data.get("name", "")
    template = data.get("template", {})

    # Validate before saving
    validation = validate_asset_template(asset_type, name, template)
    if not validation.valid:
        errors = "\n".join(f"• {e}" for e in validation.errors)
        await callback.message.edit_text(
            f"❌ <b>Ошибки в шаблоне:</b>\n{errors}",
            parse_mode="HTML",
            reply_markup=_menu_kb(),
        )
        await state.clear()
        return
    # Warnings already shown at confirm step; still allow saving

    await state.clear()

    try:
        tpl_id = await _save_template(pool, callback.from_user.id, asset_type, name, template)
        label = _TYPE_LABELS.get(asset_type, asset_type)
        msg = f"✅ Шаблон <b>«{name}»</b> ({label}) сохранён!"
        if validation.warnings:
            msg += "\n\n⚠️ <b>Замечания:</b>\n" + "\n".join(f"• {w}" for w in validation.warnings)
        await callback.message.edit_text(
            msg,
            parse_mode="HTML",
            reply_markup=_menu_kb(),
        )
    except Exception as e:
        log.exception("Failed to save asset template: %s", e)
        await callback.message.edit_text(
            "❌ Не удалось сохранить шаблон. Попробуйте позже.",
            parse_mode="HTML",
            reply_markup=_menu_kb(),
        )


# ── Apply template ─────────────────────────────────────────────────────────────

@router.callback_query(AssetTplCb.filter(F.action == "apply"))
async def cb_apply(
    callback: CallbackQuery,
    callback_data: AssetTplCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    tpl = await _get_template(pool, callback_data.tpl_id, callback.from_user.id)
    if not tpl:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await callback.answer()

    raw = tpl["template"]
    data: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})
    asset_type = tpl["asset_type"]
    tpl_name = tpl["name"]

    if asset_type in ("channel", "group"):
        accounts = await _get_active_accounts(pool, callback.from_user.id)
        if not accounts:
            await callback.message.edit_text(
                "⚠️ Нет активных аккаунтов для создания. Добавьте аккаунт.",
                reply_markup=_menu_kb(),
            )
            return

        await state.update_data(tpl_prefill=data)
        if asset_type == "channel":
            await state.set_state(ChannelFactoryFSM.choosing_account)
            action = "create_acc"
            icon = "📡"
        else:
            await state.set_state(CreateGroupFSM.choosing_account)
            action = "create_acc"
            icon = "👥"

        title_val = data.get("title", "")
        about_val = data.get("description", "") or data.get("about", "")
        kb = InlineKeyboardBuilder()
        for acc in accounts:
            name = (acc["first_name"] or "").strip()
            uname = f"@{acc['username']}" if acc.get("username") else acc.get("phone", "")
            label = f"{name} ({uname})" if name else uname
            if asset_type == "channel":
                kb.button(text=f"👤 {label}", callback_data=ChanFactCb(action=action, acc_id=acc["id"]))
            else:
                kb.button(text=f"👤 {label}", callback_data=GroupFCb(action=action, acc_id=acc["id"]))
        kb.button(text="❌ Отмена", callback_data=AssetTplCb(action="menu"))
        kb.adjust(1)

        preview = (
            f"{icon} <b>Применение шаблона «{tpl_name}»</b>\n\n"
            f"Название: <b>{html.escape(title_val or '—')}</b>\n"
            f"Описание: <b>{html.escape(about_val or '—')}</b>\n\n"
            "Выберите аккаунт для создания:"
        )
        await callback.message.edit_text(preview, parse_mode="HTML", reply_markup=kb.as_markup())

    elif asset_type == "post":
        text_val = data.get("text", "")
        await state.update_data(tpl_prefill=data)
        kb = InlineKeyboardBuilder()
        kb.button(text="📢 Создать рассылку", callback_data=MassPubCb(action="start"))
        kb.button(text="◀️ Назад к шаблонам", callback_data=AssetTplCb(action="menu"))
        kb.adjust(1)
        preview = html.escape(text_val[:500]) if text_val else "—"
        await callback.message.edit_text(
            f"📝 <b>Шаблон поста «{html.escape(tpl_name)}»</b>\n\n"
            f"<i>Превью:</i>\n{preview}\n\n"
            "Нажмите «Создать рассылку» — текст будет подставлен автоматически.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    elif asset_type == "operation":
        op_type = data.get("op_type", "")
        _OP_LABELS = {
            "mass_publish": "📤 Массовая публикация",
            "bulk_join": "🔗 Массовый join",
            "bulk_leave": "🚪 Массовый leave",
            "bulk_bot_edit": "✏️ Редактирование ботов",
        }
        op_label = _OP_LABELS.get(op_type, op_type)

        lines = [f"⚙️ <b>Шаблон операции «{html.escape(tpl_name)}»</b>\n", f"Тип: {op_label}"]
        if op_type == "mass_publish":
            text_val = data.get("text", "")
            lines.append(f"Текст: <i>{html.escape(text_val[:200])}</i>")
            await state.update_data(tpl_prefill=data)
            kb = InlineKeyboardBuilder()
            kb.button(text="📤 Запустить публикацию", callback_data=MassPubCb(action="start"))
            kb.button(text="◀️ Назад", callback_data=AssetTplCb(action="menu"))
            kb.adjust(1)
        elif op_type == "bulk_join":
            links = data.get("links", [])
            lines.append(f"Каналов: {len(links)}")
            for ln in links[:5]:
                lines.append(f"  • {html.escape(ln)}")
            await state.update_data(bj_links=links)
            await state.set_state(BulkJoinFSM.choosing_accounts)
            accounts = await _get_active_accounts(pool, callback.from_user.id)
            kb = InlineKeyboardBuilder()
            kb.button(text="👥 Все аккаунты", callback_data=MassOpCb(action="bj_accs", op_type="all"))
            for acc in accounts[:8]:
                from bot.utils.op_helpers import _acc_label
                kb.button(text=f"👤 {_acc_label(acc)}", callback_data=MassOpCb(action="bj_accs", op_id=acc["id"]))
            kb.button(text="❌ Отмена", callback_data=AssetTplCb(action="menu"))
            kb.adjust(1)
        elif op_type == "bulk_leave":
            channels = data.get("channels", [])
            lines.append(f"Каналов: {len(channels)}")
            for ch in channels[:5]:
                lines.append(f"  • {html.escape(ch)}")
            await state.update_data(bl_channels=channels)
            await state.set_state(BulkLeaveFSM.choosing_accounts)
            accounts = await _get_active_accounts(pool, callback.from_user.id)
            kb = InlineKeyboardBuilder()
            kb.button(text="👥 Все аккаунты", callback_data=MassOpCb(action="bl_accs", op_type="all"))
            for acc in accounts[:8]:
                from bot.utils.op_helpers import _acc_label
                kb.button(text=f"👤 {_acc_label(acc)}", callback_data=MassOpCb(action="bl_accs", op_id=acc["id"]))
            kb.button(text="❌ Отмена", callback_data=AssetTplCb(action="menu"))
            kb.adjust(1)
        else:  # bulk_bot_edit
            field = data.get("field", "")
            value = data.get("value", "")
            lines.append(f"Поле: <code>{html.escape(field)}</code>")
            lines.append(f"Значение: <i>{html.escape(value[:200])}</i>")
            kb = InlineKeyboardBuilder()
            kb.button(text="✏️ Перейти к редактированию", callback_data=MassOpCb(action="bulk_bot_edit"))
            kb.button(text="◀️ Назад", callback_data=AssetTplCb(action="menu"))
            kb.adjust(1)

        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

    else:  # bot — pick which managed bot to apply to
        bots = await pool.fetch(
            "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY first_name",
            callback.from_user.id,
        )
        if not bots:
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ Назад к шаблонам", callback_data=AssetTplCb(action="menu"))
            await callback.message.edit_text(
                "⚠️ У вас нет управляемых ботов.\nДобавьте бота через /start → Добавить бота.",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )
            return

        tpl_preview = []
        if data.get("name"):       tpl_preview.append(f"📛 Имя: <b>{html.escape(data['name'])}</b>")
        if data.get("description"):tpl_preview.append(f"📄 Описание: {len(data['description'])} симв.")
        if data.get("short_description"): tpl_preview.append(f"📃 Краткое: {len(data['short_description'])} симв.")
        cmds = data.get("commands") or []
        if cmds: tpl_preview.append(f"🤖 Команд: {len(cmds)}")

        kb = InlineKeyboardBuilder()
        for bot_row in bots:
            name = bot_row["first_name"] or ""
            uname = f"@{bot_row['username']}" if bot_row.get("username") else f"id{bot_row['bot_id']}"
            label = f"{name} ({uname})" if name else uname
            kb.button(
                text=f"🤖 {label[:40]}",
                callback_data=TplBotApplyCb(tpl_id=callback_data.tpl_id, bot_id=bot_row["bot_id"]),
            )
        kb.adjust(1)
        kb.button(text="❌ Отмена", callback_data=AssetTplCb(action="menu"))

        preview_text = "\n".join(tpl_preview) if tpl_preview else "—"
        await callback.message.edit_text(
            f"🤖 <b>Применить шаблон «{html.escape(tpl_name)}»</b>\n\n"
            f"Будет применено к боту:\n{preview_text}\n\n"
            "Выберите бота для применения:",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )


# ── Delete flow ────────────────────────────────────────────────────────────────

@router.callback_query(AssetTplCb.filter(F.action == "delete_confirm"))
async def cb_delete_confirm(
    callback: CallbackQuery,
    callback_data: AssetTplCb,
    pool: asyncpg.Pool,
) -> None:
    tpl = await _get_template(pool, callback_data.tpl_id, callback.from_user.id)
    if not tpl:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"🗑️ Вы уверены, что хотите удалить шаблон <b>«{tpl['name']}»</b>?",
        parse_mode="HTML",
        reply_markup=_delete_confirm_kb(callback_data.tpl_id, callback_data.asset_type or ""),
    )


@router.callback_query(AssetTplCb.filter(F.action == "delete"))
async def cb_delete(
    callback: CallbackQuery,
    callback_data: AssetTplCb,
    pool: asyncpg.Pool,
) -> None:
    deleted = await _delete_template(pool, callback_data.tpl_id, callback.from_user.id)
    if not deleted:
        await callback.answer("Не удалось удалить шаблон.", show_alert=True)
        return
    await callback.answer("✅ Шаблон удалён.")

    asset_type = callback_data.asset_type or ""
    label = _TYPE_LABELS.get(asset_type, asset_type)
    templates = await _get_templates(pool, callback.from_user.id, asset_type)
    count = len(templates)
    text = (
        f"✅ Шаблон удалён. Осталось: {count}"
        if count else
        f"✅ Шаблон удалён. Шаблонов {label} больше нет."
    )
    await callback.message.edit_text(
        f"📄 <b>Шаблоны: {label}</b>\n\n{text}",
        parse_mode="HTML",
        reply_markup=_list_kb(templates, asset_type),
    )


# ── Bot template execution ──────────────────────────────────────────────────────

@router.callback_query(TplBotApplyCb.filter())
async def cb_apply_bot_exec(
    callback: CallbackQuery,
    callback_data: TplBotApplyCb,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
) -> None:
    await callback.answer("⏳ Применяю шаблон...")
    user_id = callback.from_user.id
    bot_id = callback_data.bot_id
    tpl_id = callback_data.tpl_id
    preset_key = callback_data.preset_key

    # Load template data
    if preset_key:
        from services.preset_templates import get_preset_by_key
        parts = preset_key.split("__", 1)
        preset = get_preset_by_key(preset_key)
        data = preset["template"] if preset else {}
        tpl_name = preset["name"] if preset else "preset"
    else:
        tpl = await _get_template(pool, tpl_id, user_id)
        if not tpl:
            await callback.answer("Шаблон не найден.", show_alert=True)
            return
        raw = tpl["template"]
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        tpl_name = tpl["name"]

    # Load bot token
    bot_row = await pool.fetchrow(
        "SELECT token, username, first_name FROM managed_bots WHERE bot_id=$1 AND added_by=$2",
        bot_id, user_id,
    )
    if not bot_row:
        await callback.answer("Бот не найден.", show_alert=True)
        return

    from services import bot_api
    token = bot_row["token"]
    results = []

    if data.get("name"):
        ok = await bot_api.set_name(http, token, data["name"])
        results.append(f"📛 Имя: {'✅' if ok else '❌'}")

    if data.get("description"):
        ok = await bot_api.set_description(http, token, data["description"])
        results.append(f"📄 Описание: {'✅' if ok else '❌'}")

    if data.get("short_description"):
        ok = await bot_api.set_short_description(http, token, data["short_description"])
        results.append(f"📃 Краткое описание: {'✅' if ok else '❌'}")

    if data.get("commands"):
        cmds = data["commands"]
        try:
            ok = await bot_api.set_my_commands(http, token, cmds)
        except Exception:
            ok = False
        results.append(f"🤖 Команды ({len(cmds)} шт.): {'✅' if ok else '❌'}")

    # Create auto-replies if template has auto_replies
    if data.get("auto_replies"):
        ar_list = data["auto_replies"]
        ok_count = 0
        for ar in ar_list:
            try:
                await db.add_auto_reply(pool, bot_id, "keyword", ar["keyword"], ar["response"])
                ok_count += 1
            except Exception as e:
                log.warning("Failed to create auto_reply %s: %s", ar.get("keyword"), e)
        results.append(f"💬 Авто-ответы на команды ({ok_count}/{len(ar_list)} шт.): {'✅' if ok_count == len(ar_list) else '⚠️'}")

    # Create funnel if template has funnel_steps
    if data.get("funnel_steps"):
        steps = data["funnel_steps"]
        try:
            funnel_id = await db.create_funnel(
                pool, bot_id,
                f"{tpl_name} — Автоворонка",
                "join", None,
            )
            for i, step in enumerate(steps):
                delay_minutes = int(step.get("delay_hours", 0) * 60)
                await db.add_funnel_step(pool, funnel_id, i, step["message"], delay_minutes)
            results.append(f"🔄 Воронка: ✅ ({len(steps)} шагов)")
        except Exception as e:
            log.warning("Failed to create funnel from template: %s", e)
            results.append("🔄 Воронка: ⚠️ не удалось создать")

    # Generate admin access token for this bot
    from services.presence_setup import generate_admin_token
    from bot.callbacks import BotAdminCb
    admin_token = generate_admin_token()
    try:
        await db.upsert_bot_admin_session(pool, bot_id, user_id, admin_token)
        results.append(f"🔑 Токен управления: создан")
    except Exception as e:
        log.warning("Failed to create admin token for bot %s: %s", bot_id, e)
        admin_token = None

    bot_display = f"@{bot_row['username']}" if bot_row.get("username") else bot_row.get("first_name") or f"id{bot_id}"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔧 Admin панель бота", callback_data=BotAdminCb(action="panel", bot_id=bot_id))
    kb.button(text="◀️ Назад к шаблонам", callback_data=AssetTplCb(action="menu"))
    kb.adjust(1)

    token_line = ""
    if admin_token:
        token_line = (
            f"\n\n🔑 <b>Команда управления ботом</b> (введите в боте):\n"
            f"<code>/admin {admin_token}</code>"
        )

    await callback.message.edit_text(
        f"🤖 <b>Шаблон «{html.escape(tpl_name)}» применён к {html.escape(bot_display)}</b>\n\n"
        + "\n".join(results or ["Нечего применять."])
        + token_line,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ══════════════════════════════════════════════════════════════════
# LIBRARY — ready-made preset templates
# ══════════════════════════════════════════════════════════════════

_LIB_TYPE_LABELS = {
    "channel":   "📡 Каналы",
    "group":     "👥 Группы",
    "bot":       "🤖 Боты",
    "post":      "📝 Посты",
}
_LIB_PAGE_SIZE = 5


@router.callback_query(LibCb.filter(F.action == "menu"))
async def cb_lib_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    kb = InlineKeyboardBuilder()
    for atype, label in _LIB_TYPE_LABELS.items():
        kb.button(text=label, callback_data=LibCb(action="type", asset_type=atype))
    kb.button(text="◀️ Назад к шаблонам", callback_data=AssetTplCb(action="menu"))
    kb.adjust(2, 2, 1)
    await callback.message.edit_text(
        "📚 <b>Библиотека готовых шаблонов</b>\n\n"
        "Готовые шаблоны для быстрого старта. "
        "Выберите категорию, просмотрите шаблон и примените или клонируйте в свои.\n\n"
        "Доступно шаблонов: 23 (каналы, группы, боты, посты)",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(LibCb.filter(F.action == "type"))
async def cb_lib_type(callback: CallbackQuery, callback_data: LibCb) -> None:
    await callback.answer()
    from services.preset_templates import get_presets
    atype = callback_data.asset_type or "channel"
    presets = get_presets(atype)
    label = _LIB_TYPE_LABELS.get(atype, atype)

    kb = InlineKeyboardBuilder()
    for p in presets:
        kb.button(
            text=p["name"],
            callback_data=LibCb(action="preview", asset_type=atype, preset_key=f"{atype}__{p['id']}"),
        )
    kb.adjust(1)
    kb.button(text="◀️ Библиотека", callback_data=LibCb(action="menu"))

    await callback.message.edit_text(
        f"📚 <b>Библиотека — {label}</b>\n\n"
        f"Выберите шаблон для просмотра:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(LibCb.filter(F.action == "preview"))
async def cb_lib_preview(callback: CallbackQuery, callback_data: LibCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.preset_templates import get_preset_by_key
    key = callback_data.preset_key or ""
    preset = get_preset_by_key(key)
    if not preset:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return

    atype = callback_data.asset_type or key.split("__")[0]
    tdata = preset["template"]

    lines = [f"📄 <b>{preset['name']}</b>", f"<i>{preset['description']}</i>\n"]

    if atype in ("channel", "group"):
        if tdata.get("title"):   lines.append(f"📛 Название: <b>{html.escape(tdata['title'])}</b>")
        if tdata.get("description"):
            desc_preview = html.escape(tdata["description"][:200])
            lines.append(f"📄 Описание:\n<i>{desc_preview}</i>")
    elif atype == "bot":
        if tdata.get("name"):   lines.append(f"📛 Имя бота: <b>{html.escape(tdata['name'])}</b>")
        if tdata.get("short_description"):
            lines.append(f"📃 Краткое: <i>{html.escape(tdata['short_description'])}</i>")
        if tdata.get("description"):
            lines.append(f"📄 Описание ({len(tdata['description'])} симв.)")
        cmds = tdata.get("commands") or []
        if cmds:
            cmd_lines = "\n".join(f"  /{c['command']} — {c.get('description', '')}" for c in cmds[:5])
            lines.append(f"🤖 Команды ({len(cmds)}):\n{cmd_lines}")
        if tdata.get("welcome_message"):
            wm = html.escape(tdata["welcome_message"][:200])
            lines.append(f"\n💬 Приветствие:\n<i>{wm}…</i>")
    elif atype == "post":
        txt = html.escape((tdata.get("text") or "")[:400])
        lines.append(f"📝 Текст:\n<i>{txt}</i>")

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Клонировать в мои",
              callback_data=LibCb(action="clone", asset_type=atype, preset_key=key))
    kb.button(text="🚀 Применить сейчас",
              callback_data=LibCb(action="apply", asset_type=atype, preset_key=key))
    kb.button(text="◀️ Назад",
              callback_data=LibCb(action="type", asset_type=atype))
    kb.adjust(2, 1)
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(LibCb.filter(F.action == "clone"))
async def cb_lib_clone(callback: CallbackQuery, callback_data: LibCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    from services.preset_templates import get_preset_by_key
    key = callback_data.preset_key or ""
    preset = get_preset_by_key(key)
    if not preset:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    atype = callback_data.asset_type or key.split("__")[0]

    tpl_id = await _save_template(pool, callback.from_user.id, atype, preset["name"], preset["template"])

    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Открыть шаблон",
              callback_data=AssetTplCb(action="view", tpl_id=tpl_id, asset_type=atype))
    kb.button(text="🚀 Применить",
              callback_data=AssetTplCb(action="apply", tpl_id=tpl_id, asset_type=atype))
    kb.button(text="📚 Библиотека",
              callback_data=LibCb(action="menu"))
    kb.adjust(2, 1)
    await callback.message.edit_text(
        f"✅ <b>Шаблон «{html.escape(preset['name'])}» скопирован в ваши шаблоны!</b>\n\n"
        "Теперь вы можете его редактировать и применять.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(LibCb.filter(F.action == "apply"))
async def cb_lib_apply(
    callback: CallbackQuery, callback_data: LibCb,
    pool: asyncpg.Pool, state: FSMContext,
) -> None:
    await callback.answer()
    from services.preset_templates import get_preset_by_key
    key = callback_data.preset_key or ""
    preset = get_preset_by_key(key)
    if not preset:
        await callback.answer("Шаблон не найден.", show_alert=True)
        return
    atype = callback_data.asset_type or key.split("__")[0]
    data = preset["template"]
    tpl_name = preset["name"]

    if atype in ("channel", "group"):
        accounts = await _get_active_accounts(pool, callback.from_user.id)
        if not accounts:
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ Назад", callback_data=LibCb(action="menu"))
            await callback.message.edit_text(
                "⚠️ Нет активных аккаунтов. Добавьте аккаунт.",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )
            return
        await state.update_data(tpl_prefill=data)
        if atype == "channel":
            await state.set_state(ChannelFactoryFSM.choosing_account)
            action_key = "create_acc"
            icon = "📡"
        else:
            from bot.states import CreateGroupFSM
            await state.set_state(CreateGroupFSM.choosing_account)
            action_key = "create_acc"
            icon = "👥"
        kb = InlineKeyboardBuilder()
        for acc in accounts:
            name = (acc["first_name"] or "").strip()
            uname = f"@{acc['username']}" if acc.get("username") else acc.get("phone", "")
            label = f"{name} ({uname})" if name else uname
            if atype == "channel":
                kb.button(text=f"👤 {label}", callback_data=ChanFactCb(action=action_key, acc_id=acc["id"]))
            else:
                kb.button(text=f"👤 {label}", callback_data=GroupFCb(action=action_key, acc_id=acc["id"]))
        kb.button(text="❌ Отмена", callback_data=LibCb(action="menu"))
        kb.adjust(1)
        title_val = data.get("title", "")
        await callback.message.edit_text(
            f"{icon} <b>Создать по шаблону «{html.escape(tpl_name)}»</b>\n\n"
            f"Название: <b>{html.escape(title_val or '—')}</b>\n\n"
            "Выберите аккаунт для создания:",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif atype == "post":
        await state.update_data(tpl_prefill=data)
        kb = InlineKeyboardBuilder()
        kb.button(text="📢 Создать рассылку", callback_data=MassPubCb(action="start"))
        kb.button(text="◀️ Назад", callback_data=LibCb(action="menu"))
        kb.adjust(1)
        preview = html.escape((data.get("text") or "")[:300])
        await callback.message.edit_text(
            f"📝 <b>Шаблон поста «{html.escape(tpl_name)}»</b>\n\n"
            f"<i>{preview}</i>\n\n"
            "Нажмите «Создать рассылку» — текст подставится автоматически.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )

    elif atype == "bot":
        # Bot: pick which managed bot to apply to
        bots = await pool.fetch(
            "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
        if not bots:
            kb = InlineKeyboardBuilder()
            kb.button(text="◀️ Назад", callback_data=LibCb(action="menu"))
            await callback.message.edit_text(
                "⚠️ У вас нет управляемых ботов.",
                parse_mode="HTML", reply_markup=kb.as_markup(),
            )
            return
        kb = InlineKeyboardBuilder()
        for bot_row in bots:
            name = bot_row["first_name"] or ""
            uname = f"@{bot_row['username']}" if bot_row.get("username") else f"id{bot_row['bot_id']}"
            label = f"{name} ({uname})" if name else uname
            kb.button(
                text=f"🤖 {label[:40]}",
                callback_data=TplBotApplyCb(tpl_id=0, bot_id=bot_row["bot_id"], preset_key=key),
            )
        kb.adjust(1)
        kb.button(text="❌ Отмена", callback_data=LibCb(action="menu"))
        await callback.message.edit_text(
            f"🤖 <b>Применить шаблон «{html.escape(tpl_name)}»</b>\n\n"
            "Выберите бота:",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )


# ── Back / cancel ──────────────────────────────────────────────────────────────

@router.callback_query(AssetTplCb.filter(F.action == "back"))
async def cb_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    # Delegate back navigation to the caller; show menu as fallback
    await callback.message.edit_text(
        "📄 <b>Шаблоны ассетов</b>",
        parse_mode="HTML",
        reply_markup=_menu_kb(),
    )

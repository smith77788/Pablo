"""Asset Templates handler.

Manages reusable templates for Telegram assets:
  - Bot templates (name, description, short_description)
  - Channel templates (title, description, username)
  - Group templates (title, description, username)
  - Post templates (text with optional HTML markup)

Callback prefix: atpl
"""
from __future__ import annotations

import json
import logging
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg

from bot.callbacks import AssetTplCb
from bot.states import AssetTemplateFSM

log = logging.getLogger(__name__)
router = Router()

# ── Asset type metadata ────────────────────────────────────────────────────────

_TYPE_LABELS = {
    "bot":     "🤖 Бот",
    "channel": "📡 Канал",
    "group":   "👥 Группа",
    "post":    "📝 Пост",
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
}


# ── Keyboard helpers ───────────────────────────────────────────────────────────

def _menu_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Шаблоны ботов",    callback_data=AssetTplCb(action="list", asset_type="bot"))
    kb.button(text="📡 Шаблоны каналов",  callback_data=AssetTplCb(action="list", asset_type="channel"))
    kb.button(text="👥 Шаблоны групп",    callback_data=AssetTplCb(action="list", asset_type="group"))
    kb.button(text="📝 Шаблоны постов",   callback_data=AssetTplCb(action="list", asset_type="post"))
    kb.button(text="➕ Создать",           callback_data=AssetTplCb(action="create"))
    kb.button(text="◀️ Назад",            callback_data=AssetTplCb(action="back"))
    kb.adjust(2, 2, 2)
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
    kb.button(text="🤖 Бот",      callback_data=AssetTplCb(action="choose_type", asset_type="bot"))
    kb.button(text="📡 Канал",    callback_data=AssetTplCb(action="choose_type", asset_type="channel"))
    kb.button(text="👥 Группа",   callback_data=AssetTplCb(action="choose_type", asset_type="group"))
    kb.button(text="📝 Пост",     callback_data=AssetTplCb(action="choose_type", asset_type="post"))
    kb.button(text="◀️ Отмена",   callback_data=AssetTplCb(action="menu"))
    kb.adjust(2, 2, 1)
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
    asset_type = callback_data.asset_type
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
        reply_markup=_view_kb(callback_data.tpl_id, callback_data.asset_type),
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
    asset_type = callback_data.asset_type
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

    await message.answer(
        f"➕ <b>Шаблон {label} — шаг 2/2</b>\n\n{prompt}",
        parse_mode="HTML",
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

    await state.update_data(template=template)
    await state.set_state(AssetTemplateFSM.confirming)

    label = _TYPE_LABELS.get(asset_type, asset_type)
    lines = [f"✅ <b>Проверьте шаблон</b>", f"Тип: {label}", f"Название: <b>{name}</b>"]
    for k, v in template.items():
        if v:
            lines.append(f"<b>{k}:</b> {v}")

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
    asset_type = data.get("asset_type", callback_data.asset_type)
    name = data.get("name", "")
    template = data.get("template", {})
    await state.clear()

    try:
        tpl_id = await _save_template(pool, callback.from_user.id, asset_type, name, template)
        label = _TYPE_LABELS.get(asset_type, asset_type)
        await callback.message.edit_text(
            f"✅ Шаблон <b>«{name}»</b> ({label}) сохранён!",
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


# ── Apply (stub) ───────────────────────────────────────────────────────────────

@router.callback_query(AssetTplCb.filter(F.action == "apply"))
async def cb_apply(callback: CallbackQuery, callback_data: AssetTplCb) -> None:
    await callback.answer(
        "🚧 В разработке — применение шаблонов при создании ассетов",
        show_alert=True,
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
        reply_markup=_delete_confirm_kb(callback_data.tpl_id, callback_data.asset_type),
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

    asset_type = callback_data.asset_type
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

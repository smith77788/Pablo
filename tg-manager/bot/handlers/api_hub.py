"""Compute API Hub — per-user API key management."""

from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ApiHubCb, BmCb
from bot.utils.picker_cap import cap_text as _cap_text, cap_text_append as _cap_text_note
from bot.utils.picker_cap import cap_slice as _cap, cap_note_button as _cap_note
from bot.states import ApiKeyFSM
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_MAX_KEYS = 5


async def _list_keys(pool: asyncpg.Pool, user_id: int) -> list[dict]:
    try:
        rows = await pool.fetch(
            """SELECT id, key_prefix, name, created_at, last_used_at, requests_total
               FROM api_keys
               WHERE user_id=$1 AND is_active=TRUE
               ORDER BY created_at DESC
               LIMIT $2""",
            user_id,
            _MAX_KEYS,
        )
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("api_hub._list_keys: %s", e)
        return []


async def _count_keys(pool: asyncpg.Pool, user_id: int) -> int:
    try:
        return int(await pool.fetchval(
            "SELECT COUNT(*) FROM api_keys WHERE user_id=$1 AND is_active=TRUE",
            user_id,
        ) or 0)
    except Exception:
        return 0


def _menu_text(keys: list[dict]) -> str:
    lines = ["🔑 <b>Compute API</b>\n"]
    if not keys:
        lines.append(
            "<i>У вас нет активных API-ключей.</i>\n\n"
            "Создайте ключ для программного доступа к Infragram:\n"
            "запускайте операции, проверяйте статус и управляйте "
            "инфраструктурой через REST API.\n\n"
            "<b>Базовый URL:</b> <code>/api/v1/</code>\n"
            "<b>Аутентификация:</b> <code>X-Api-Key: bm_xxxxxxxx...</code>"
        )
    else:
        lines.append("Активные ключи:")
        _shown_t = _cap_text(keys)
        for k in _shown_t:
            created = k["created_at"].strftime("%d.%m.%Y")
            last    = k["last_used_at"].strftime("%d.%m %H:%M") if k.get("last_used_at") else "—"
            reqs    = int(k["requests_total"] or 0)
            lines.append(
                f"\n• <code>{k['key_prefix']}…</code> «{k['name']}»\n"
                f"  Создан: {created} · Использован: {last} · Запросов: {reqs}"
            )
        _cap_text_note(lines, keys, _shown_t, "ключей")
        lines.append(
            "\n\n<b>API:</b> <code>GET /api/v1/operations</code>\n"
            "<b>Аутентификация:</b> <code>X-Api-Key: bm_xxxxxxxx...</code>"
        )
    return "\n".join(lines)


@router.callback_query(ApiHubCb.filter(F.action == "menu"))
async def cb_api_menu(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await safe_answer(callback)
    await state.clear()
    keys = await _list_keys(pool, callback.from_user.id)
    text = _menu_text(keys)

    kb = InlineKeyboardBuilder()
    _shown = _cap(keys)
    for k in _shown:
        kb.button(
            text=f"🗑 {k['key_prefix']}… «{k['name']}»",
            callback_data=ApiHubCb(action="revoke", item_id=k["id"]),
        )
    _cap_note(kb, keys, _shown, what="acc")
    count = len(keys)
    if count < _MAX_KEYS:
        kb.button(text="➕ Создать ключ", callback_data=ApiHubCb(action="create"))
    kb.button(text="📖 Документация API", callback_data=ApiHubCb(action="docs"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="settings"))
    kb.adjust(*(1 for _ in range(count)), 1, 1, 1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ApiHubCb.filter(F.action == "create"))
async def cb_api_create(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await safe_answer(callback)
    count = await _count_keys(pool, callback.from_user.id)
    if count >= _MAX_KEYS:
        await callback.answer(f"Максимум {_MAX_KEYS} ключей.", show_alert=True)
        return

    await state.set_state(ApiKeyFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=ApiHubCb(action="menu"))
    await callback.message.edit_text(
        "🔑 <b>Создать API-ключ</b>\n\n"
        "Введите название ключа (например: <code>n8n workflow</code>):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(ApiKeyFSM.waiting_name, F.text)
async def msg_api_key_name(
    message: Message,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    name = message.text.strip()[:64]
    if not name:
        await message.answer("Введите непустое название.")
        return

    await state.clear()

    # Выдача — одна на бот и мини-апп (services/api_keys.py): ключ, который
    # выдают здесь, обязан приниматься REST API, а раньше не принимался.
    from services import api_keys as _keys
    try:
        created = await _keys.create_key(pool, message.from_user.id, name)
    except ValueError as e:
        await message.answer(str(e))
        return
    except Exception as e:
        log.warning("api_hub: key create error: %s", e)
        await message.answer("❌ Ошибка создания ключа. Попробуйте позже.")
        return
    raw_key = created["key"]

    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 К API ключам", callback_data=ApiHubCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>API-ключ создан!</b>\n\n"
        f"<code>{raw_key}</code>\n\n"
        f"⚠️ <b>Сохраните ключ — он показывается только один раз!</b>\n\n"
        f"<b>Использование:</b>\n"
        f"<code>curl -H 'X-Api-Key: {raw_key}' \\\n"
        f"     /api/v1/me</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ApiHubCb.filter(F.action == "revoke"))
async def cb_api_revoke(
    callback: CallbackQuery,
    callback_data: ApiHubCb,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
    key_id = callback_data.item_id

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 Да, отозвать",
        callback_data=ApiHubCb(action="revoke_confirm", item_id=key_id),
    )
    kb.button(text="◀️ Отмена", callback_data=ApiHubCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "⚠️ Вы уверены, что хотите отозвать этот API-ключ?\n"
        "Все интеграции использующие этот ключ перестанут работать.",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ApiHubCb.filter(F.action == "revoke_confirm"))
async def cb_api_revoke_confirm(
    callback: CallbackQuery,
    callback_data: ApiHubCb,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
    key_id = callback_data.item_id
    try:
        await pool.execute(
            "UPDATE api_keys SET is_active=FALSE WHERE id=$1 AND user_id=$2",
            key_id,
            callback.from_user.id,
        )
    except Exception as e:
        log.warning("api_hub: revoke error: %s", e)

    keys = await _list_keys(pool, callback.from_user.id)
    text = _menu_text(keys)
    kb   = InlineKeyboardBuilder()
    _shown = _cap(keys)
    for k in _shown:
        kb.button(
            text=f"🗑 {k['key_prefix']}… «{k['name']}»",
            callback_data=ApiHubCb(action="revoke", item_id=k["id"]),
        )
    _cap_note(kb, keys, _shown, what="acc")
    count = len(keys)
    if count < _MAX_KEYS:
        kb.button(text="➕ Создать ключ", callback_data=ApiHubCb(action="create"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="settings"))
    kb.adjust(*(1 for _ in range(count)), 1, 1)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(ApiHubCb.filter(F.action == "docs"))
async def cb_api_docs(
    callback: CallbackQuery,
) -> None:
    await safe_answer(callback)
    text = (
        "📖 <b>Infragram Compute API</b>\n\n"
        "<b>Аутентификация:</b>\n"
        "<code>X-Api-Key: bm_your_key</code>\n\n"
        "Владелец берётся из ключа — owner_id передавать не нужно.\n\n"
        "<b>Эндпоинты:</b>\n"
        "<code>GET  /api/v1/health</code>\n"
        "<code>GET  /api/v1/me</code>\n"
        "<code>GET  /api/v1/accounts</code>\n"
        "<code>GET  /api/v1/operations</code>\n"
        "<code>GET  /api/v1/operations/{id}</code>\n"
        "<code>POST /api/v1/send_message</code>\n"
        "<code>POST /api/v1/click_button</code>\n"
        "<code>GET  /api/v1/get_messages</code>\n\n"
        "<b>Пример:</b>\n"
        "<code>curl -X POST /api/v1/send_message \\\n"
        "  -H 'X-Api-Key: bm_...' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{\"account_id\":456,\n"
        "       \"chat_id\":789,\"text\":\"привет\"}'</code>\n\n"
        "Вебхуки на события и Google Sheets — в мини-аппе, раздел «Интеграции»."
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ApiHubCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

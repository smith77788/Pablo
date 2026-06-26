"""Compute API Hub — per-user API key management."""

from __future__ import annotations

import hashlib
import logging
import secrets

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import ApiHubCb, BmCb
from bot.states import ApiKeyFSM

log = logging.getLogger(__name__)
router = Router()

_MAX_KEYS = 5


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _prefix(raw: str) -> str:
    return raw[:8]


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
            "Создайте ключ для программного доступа к BotMother:\n"
            "запускайте операции, проверяйте статус и управляйте "
            "инфраструктурой через REST API.\n\n"
            "<b>Базовый URL:</b> <code>/api/v1/</code>\n"
            "<b>Аутентификация:</b> <code>X-Api-Key: bm_xxxxxxxx...</code>"
        )
    else:
        lines.append("Активные ключи:")
        for k in keys:
            created = k["created_at"].strftime("%d.%m.%Y")
            last    = k["last_used_at"].strftime("%d.%m %H:%M") if k.get("last_used_at") else "—"
            reqs    = int(k["requests_total"] or 0)
            lines.append(
                f"\n• <code>bm_{k['key_prefix']}...</code> «{k['name']}»\n"
                f"  Создан: {created} · Использован: {last} · Запросов: {reqs}"
            )
        lines.append(
            "\n\n<b>API:</b> <code>POST /api/v1/my/operations</code>\n"
            "<b>Аутентификация:</b> <code>X-Api-Key: bm_xxxxxxxx...</code>"
        )
    return "\n".join(lines)


@router.callback_query(ApiHubCb.filter(F.action == "menu"))
async def cb_api_menu(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    keys = await _list_keys(pool, callback.from_user.id)
    text = _menu_text(keys)

    kb = InlineKeyboardBuilder()
    for k in keys:
        kb.button(
            text=f"🗑 bm_{k['key_prefix']}... «{k['name']}»",
            callback_data=ApiHubCb(action="revoke", item_id=k["id"]),
        )
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
    await callback.answer()
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

    # Generate key: bm_ + 32 random URL-safe chars
    raw_key = "bm_" + secrets.token_urlsafe(32)
    key_hash   = _hash_key(raw_key)
    key_prefix = _prefix(raw_key[3:])  # prefix after "bm_"

    try:
        count = await _count_keys(pool, message.from_user.id)
        if count >= _MAX_KEYS:
            await message.answer(f"Максимум {_MAX_KEYS} ключей.")
            return

        await pool.execute(
            """INSERT INTO api_keys (user_id, key_hash, key_prefix, name)
               VALUES ($1,$2,$3,$4)""",
            message.from_user.id,
            key_hash,
            key_prefix,
            name,
        )
    except Exception as e:
        log.warning("api_hub: key create error: %s", e)
        await message.answer("❌ Ошибка создания ключа. Попробуйте позже.")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 К API ключам", callback_data=ApiHubCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ <b>API-ключ создан!</b>\n\n"
        f"<code>{raw_key}</code>\n\n"
        f"⚠️ <b>Сохраните ключ — он показывается только один раз!</b>\n\n"
        f"<b>Использование:</b>\n"
        f"<code>curl -H 'X-Api-Key: {raw_key}' \\\n"
        f"     /api/v1/health</code>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ApiHubCb.filter(F.action == "revoke"))
async def cb_api_revoke(
    callback: CallbackQuery,
    callback_data: ApiHubCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
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
    await callback.answer()
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
    for k in keys:
        kb.button(
            text=f"🗑 bm_{k['key_prefix']}... «{k['name']}»",
            callback_data=ApiHubCb(action="revoke", item_id=k["id"]),
        )
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
    await callback.answer()
    text = (
        "📖 <b>BotMother Compute API</b>\n\n"
        "<b>Аутентификация:</b>\n"
        "<code>X-Api-Key: bm_your_key</code>\n\n"
        "<b>Эндпоинты:</b>\n"
        "<code>GET  /api/v1/health</code>\n"
        "<code>GET  /api/v1/accounts</code>\n"
        "<code>POST /api/v1/send_message</code>\n"
        "<code>POST /api/v1/click_button</code>\n"
        "<code>GET  /api/v1/get_messages</code>\n\n"
        "<b>Пример:</b>\n"
        "<code>curl -X POST /api/v1/send_message \\\n"
        "  -H 'X-Api-Key: bm_...' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{\"owner_id\":123,\"account_id\":456,\n"
        "       \"chat_id\":789,\"text\":\"hello\"}'</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=ApiHubCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())

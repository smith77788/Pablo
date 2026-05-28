"""Global Presence Factory — guided FSM wizard for worldwide Telegram channel creation."""
from __future__ import annotations

import json
import logging
from typing import Optional

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import GeoPresenceCb
from bot.states import GlobalPresenceFSM
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
from database import db
from services.geo_data import GEO_PRESETS, parse_custom_geo_list
from services.presence_planner import render_pattern, build_targets, estimate_duration_minutes
from services.username_engine import slugify

log = logging.getLogger(__name__)
router = Router()

_TPL_PAGE_SIZE = 5
_ACC_PAGE_SIZE = 8


# ── Helpers ────────────────────────────────────────────────────────────────

def _back_cancel_row() -> list:
    return []


async def _edit(cb: CallbackQuery, text: str, markup=None) -> None:
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=markup, parse_mode="HTML")


async def _reply(msg: Message, text: str, markup=None) -> None:
    await msg.answer(text, reply_markup=markup, parse_mode="HTML")


def _cancel_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    return kb.as_markup()


def _back_cancel_kb(back_action: str, plan_id: int = 0) -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action=back_action, plan_id=plan_id))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2)
    return kb.as_markup()


# ── Step 1: Entry / Asset Type ─────────────────────────────────────────────

@router.callback_query(GeoPresenceCb.filter(F.action == "menu"))
async def cb_gp_menu(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Global Presence Factory", "pro"),
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    await callback.answer()
    await state.clear()
    await state.set_state(GlobalPresenceFSM.choosing_asset_type)
    kb = InlineKeyboardBuilder()
    kb.button(text="📡 Каналы", callback_data=GeoPresenceCb(action="asset", item="channel"))
    kb.button(text="👥 Группы", callback_data=GeoPresenceCb(action="asset", item="group"))
    kb.button(text="🤖 Боты 🔜", callback_data=GeoPresenceCb(action="asset", item="v2"))
    kb.button(text="📦 Пакет 🔜", callback_data=GeoPresenceCb(action="asset", item="v2"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2, 2, 1)
    await callback.message.edit_text(
        "🌍 <b>Global Presence Factory</b>\n\n"
        "Создайте Telegram-присутствие в любом городе мира за несколько шагов — "
        "каналы, группы или боты для выбранных стран и регионов с вашими шаблонами.\n\n"
        "<b>Шаг 1/8 — Тип актива</b>\n"
        "Что создаём?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "asset"), GlobalPresenceFSM.choosing_asset_type)
async def cb_gp_asset(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    asset = callback_data.item or "channel"
    if asset == "v2":
        await callback.answer("🔜 Боты и пакеты будут в V2.1. Группы уже доступны.", show_alert=True)
        return
    if asset not in ("channel", "group"):
        await callback.answer("Неподдерживаемый тип", show_alert=True)
        return
    await callback.answer()
    await state.update_data(asset_type=asset)
    await state.set_state(GlobalPresenceFSM.choosing_template)
    await _show_template_step(callback, state, pool, asset_type=asset, page=0)


# ── Step 2: Template ────────────────────────────────────────────────────────

async def _show_template_step(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
    asset_type: str = "channel", page: int = 0,
) -> None:
    user_id = callback.from_user.id
    offset = page * _TPL_PAGE_SIZE
    templates = await pool.fetch(
        "SELECT id, name FROM asset_templates WHERE owner_id=$1 AND asset_type=$2 "
        "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
        user_id, asset_type, _TPL_PAGE_SIZE + 1, offset,
    )
    has_more = len(templates) > _TPL_PAGE_SIZE
    templates = templates[:_TPL_PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for tpl in templates:
        kb.button(
            text=f"📋 {tpl['name'][:30]}",
            callback_data=GeoPresenceCb(action="sel_tpl", item=str(tpl["id"])),
        )
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=GeoPresenceCb(action="tpl_page", page=page - 1))
    if has_more:
        nav.button(text="▶️", callback_data=GeoPresenceCb(action="tpl_page", page=page + 1))
    if page > 0 or has_more:
        nav.adjust(2)
        kb.attach(nav)

    kb.button(text="⏭️ Без шаблона", callback_data=GeoPresenceCb(action="skip_tpl"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    tpl_count = len(templates) + offset
    header = f"📋 Найдено шаблонов: {tpl_count}+" if has_more else (
        f"📋 Найдено шаблонов: {len(templates) + offset}"
    )

    asset_label = "группы" if asset_type == "group" else "канала"
    await _edit(
        callback,
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 2/8 — Шаблон {asset_label}</b>\n"
        f"Шаблон задаёт описание, аватар и первый пост.\n\n"
        f"{header}\n"
        f"(Шаблоны создаются в: BotMother → Операции → Шаблоны)",
        markup=kb.as_markup(),
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "tpl_page"), GlobalPresenceFSM.choosing_template)
async def cb_gp_tpl_page(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    sd = await state.get_data()
    asset_type = sd.get("asset_type", "channel")
    await _show_template_step(callback, state, pool, asset_type=asset_type, page=callback_data.page)


@router.callback_query(GeoPresenceCb.filter(F.action == "sel_tpl"), GlobalPresenceFSM.choosing_template)
async def cb_gp_sel_tpl(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    tpl_id = int(callback_data.item or 0)
    tpl = await pool.fetchrow(
        "SELECT id, name, template FROM asset_templates WHERE id=$1 AND owner_id=$2",
        tpl_id, callback.from_user.id,
    )
    if not tpl:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    await callback.answer()
    await state.update_data(template_id=tpl_id, template_name=tpl["name"])
    await state.set_state(GlobalPresenceFSM.entering_name_pattern)
    await _show_name_pattern_step(callback, state, prefill=None)


@router.callback_query(GeoPresenceCb.filter(F.action == "skip_tpl"), GlobalPresenceFSM.choosing_template)
async def cb_gp_skip_tpl(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.update_data(template_id=None, template_name=None)
    await state.set_state(GlobalPresenceFSM.entering_name_pattern)
    await _show_name_pattern_step(callback, state, prefill=None)


# ── Step 3: Name Pattern ───────────────────────────────────────────────────

async def _show_name_pattern_step(
    callback: CallbackQuery, state: FSMContext, prefill: str | None
) -> None:
    examples = [
        "Crypto News {{CITY}}",
        "AI Jobs {{CITY}}",
        "{{CITY}} Business Hub",
        "Trading {{COUNTRY_CODE}} {{CITY}}",
    ]
    ex_text = "\n".join(f"  • <code>{e}</code>" for e in examples)
    await _edit(
        callback,
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 3/8 — Паттерн названия</b>\n"
        f"Введите шаблон для названия канала.\n\n"
        f"Доступные плейсхолдеры:\n"
        f"  <code>{{{{CITY}}}}</code> — город\n"
        f"  <code>{{{{COUNTRY}}}}</code> — страна\n"
        f"  <code>{{{{COUNTRY_CODE}}}}</code> — код страны (DE, FR…)\n"
        f"  <code>{{{{CITY_SLUG}}}}</code> — транслит-слаг города\n"
        f"  <code>{{{{INDEX}}}}</code> — порядковый номер\n\n"
        f"Примеры:\n{ex_text}\n\n"
        + (f"💡 Последний ввод: <code>{prefill}</code>\n\n" if prefill else "")
        + f"Введите паттерн:",
        markup=_cancel_kb(),
    )


@router.message(GlobalPresenceFSM.entering_name_pattern)
async def msg_gp_name_pattern(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    pattern = (message.text or "").strip()
    if not pattern:
        await _reply(message, "⚠️ Паттерн не может быть пустым. Введите снова:", _cancel_kb())
        return
    if len(pattern) > 200:
        await _reply(message, "⚠️ Слишком длинный паттерн (макс. 200 символов). Попробуйте короче:", _cancel_kb())
        return

    # Show examples before confirming
    sample_geos = [
        {"city": "Berlin", "city_slug": "berlin", "country": "Germany", "country_code": "de", "index": 1},
        {"city": "Paris", "city_slug": "paris", "country": "France", "country_code": "fr", "index": 2},
        {"city": "Madrid", "city_slug": "madrid", "country": "Spain", "country_code": "es", "index": 3},
    ]
    examples_text = "\n".join(
        f"  📡 <b>{render_pattern(pattern, g)}</b>"
        for g in sample_geos
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=GeoPresenceCb(action="accept_name"))
    kb.button(text="✏️ Изменить", callback_data=GeoPresenceCb(action="retry_name"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2, 1)

    await state.update_data(name_pattern_pending=pattern)
    await _reply(
        message,
        f"🌍 <b>Предпросмотр паттерна названия</b>\n\n"
        f"Паттерн: <code>{pattern}</code>\n\n"
        f"Примеры:\n{examples_text}\n\n"
        f"Всё верно?",
        markup=kb.as_markup(),
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "accept_name"), GlobalPresenceFSM.entering_name_pattern)
async def cb_gp_accept_name(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    await state.update_data(name_pattern=sd.get("name_pattern_pending", ""), name_pattern_pending=None)
    await state.set_state(GlobalPresenceFSM.entering_username_pattern)
    await _show_username_pattern_step(callback, state, prefill=None)


@router.callback_query(GeoPresenceCb.filter(F.action == "retry_name"), GlobalPresenceFSM.entering_name_pattern)
async def cb_gp_retry_name(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    await _show_name_pattern_step(callback, state, prefill=sd.get("name_pattern_pending"))


# ── Step 4: Username Pattern ────────────────────────────────────────────────

async def _show_username_pattern_step(
    callback: CallbackQuery, state: FSMContext, prefill: str | None
) -> None:
    examples = [
        "crypto_{{CITY_SLUG}}",
        "ai_jobs_{{CITY_SLUG}}",
        "trading_{{COUNTRY_CODE}}_{{CITY_SLUG}}",
        "{{CITY_SLUG}}_news",
    ]
    ex_text = "\n".join(f"  • <code>{e}</code>" for e in examples)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭️ Без username", callback_data=GeoPresenceCb(action="skip_uname"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)
    await _edit(
        callback,
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 4/8 — Паттерн username</b>\n"
        f"Username делает канал публичным и находимым.\n"
        f"Правила: 5–32 символа, a-z, 0-9, подчёркивание.\n\n"
        f"Примеры:\n{ex_text}\n\n"
        + (f"💡 Последний ввод: <code>{prefill}</code>\n\n" if prefill else "")
        + f"Введите паттерн или пропустите:",
        markup=kb.as_markup(),
    )


@router.message(GlobalPresenceFSM.entering_username_pattern)
async def msg_gp_username_pattern(message: Message, state: FSMContext) -> None:
    pattern = (message.text or "").strip()
    if not pattern:
        await _reply(message, "⚠️ Введите паттерн или нажмите «Без username».", _cancel_kb())
        return

    sample_geos = [
        {"city": "Berlin", "city_slug": "berlin", "country": "Germany", "country_code": "de", "index": 1},
        {"city": "Paris", "city_slug": "paris", "country": "France", "country_code": "fr", "index": 2},
        {"city": "Madrid", "city_slug": "madrid", "country": "Spain", "country_code": "es", "index": 3},
    ]
    examples_text = "\n".join(
        f"  @<b>{slugify(render_pattern(pattern, g))[:32]}</b>"
        for g in sample_geos
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=GeoPresenceCb(action="accept_uname"))
    kb.button(text="✏️ Изменить", callback_data=GeoPresenceCb(action="retry_uname"))
    kb.button(text="⏭️ Без username", callback_data=GeoPresenceCb(action="skip_uname"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2, 2)

    await state.update_data(username_pattern_pending=pattern)
    await _reply(
        message,
        f"🌍 <b>Предпросмотр паттерна username</b>\n\n"
        f"Паттерн: <code>{pattern}</code>\n\n"
        f"Примеры:\n{examples_text}\n\n"
        f"⚠️ Telegram проверяет доступность username при создании. "
        f"Если занят — система попробует варианты.\n\n"
        f"Всё верно?",
        markup=kb.as_markup(),
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "accept_uname"), GlobalPresenceFSM.entering_username_pattern)
async def cb_gp_accept_uname(
    callback: CallbackQuery, callback_data: GeoPresenceCb, state: FSMContext,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    await state.update_data(username_pattern=sd.get("username_pattern_pending", ""), username_pattern_pending=None)
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


@router.callback_query(GeoPresenceCb.filter(F.action == "retry_uname"), GlobalPresenceFSM.entering_username_pattern)
async def cb_gp_retry_uname(callback: CallbackQuery, callback_data: GeoPresenceCb, state: FSMContext) -> None:
    await callback.answer()
    sd = await state.get_data()
    await _show_username_pattern_step(callback, state, prefill=sd.get("username_pattern_pending"))


@router.callback_query(GeoPresenceCb.filter(F.action == "skip_uname"))
async def cb_gp_skip_uname(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(username_pattern=None, username_pattern_pending=None)
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


# ── Step 5: Geo Selection ──────────────────────────────────────────────────

async def _show_geo_step(callback: CallbackQuery, state: FSMContext) -> None:
    kb = InlineKeyboardBuilder()
    for key, preset in GEO_PRESETS.items():
        kb.button(
            text=f"{preset['label']} ({preset['count']})",
            callback_data=GeoPresenceCb(action="geo", item=key),
        )
    kb.button(text="✏️ Ввести города вручную", callback_data=GeoPresenceCb(action="geo_custom"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_uname"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)
    await _edit(
        callback,
        "🌍 <b>Global Presence Factory</b>\n\n"
        "<b>Шаг 5/8 — География</b>\n"
        "Выберите пресет или введите города вручную:",
        markup=kb.as_markup(),
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "geo"), GlobalPresenceFSM.choosing_geo)
async def cb_gp_geo_preset(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    preset_key = callback_data.item or ""
    preset = GEO_PRESETS.get(preset_key)
    if not preset:
        await callback.answer("Пресет не найден", show_alert=True)
        return
    await callback.answer()
    await state.update_data(geo_preset=preset_key, geo_list=preset["cities"])
    await state.set_state(GlobalPresenceFSM.choosing_accounts)
    await _show_accounts_step(callback, state, pool, page=0)


@router.callback_query(GeoPresenceCb.filter(F.action == "geo_custom"), GlobalPresenceFSM.choosing_geo)
async def cb_gp_geo_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.entering_custom_geo)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_geo"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2)
    await _edit(
        callback,
        "🌍 <b>Global Presence Factory</b>\n\n"
        "<b>Шаг 5/8 — Кастомные города</b>\n"
        "Введите города, по одному на строку:\n\n"
        "<code>Berlin\nParis\nMadrid\nTokyo</code>\n\n"
        "Можно указать детали через запятую:\n"
        "<code>Berlin, Germany, de</code>",
        markup=kb.as_markup(),
    )


@router.message(GlobalPresenceFSM.entering_custom_geo)
async def msg_gp_custom_geo(
    message: Message, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await _reply(message, "⚠️ Введите хотя бы один город.", _cancel_kb())
        return
    geo_list = parse_custom_geo_list(text)
    if not geo_list:
        await _reply(message, "⚠️ Не удалось распознать ни одного города. Введите снова.", _cancel_kb())
        return
    await state.update_data(geo_preset="custom", geo_list=geo_list)
    await state.set_state(GlobalPresenceFSM.choosing_accounts)

    class FakeCallback:
        from_user = message.from_user
        message = message
        async def answer(self, *a, **kw): pass

    await _show_accounts_step(FakeCallback(), state, pool, page=0, send_new=True, original_message=message)


# ── Step 6: Account Selection ──────────────────────────────────────────────

async def _show_accounts_step(
    callback, state: FSMContext, pool: asyncpg.Pool,
    page: int = 0, send_new: bool = False, original_message: Message | None = None,
) -> None:
    user_id = callback.from_user.id
    sd = await state.get_data()
    selected_ids: list[int] = sd.get("selected_acc_ids") or []

    offset = page * _ACC_PAGE_SIZE
    accounts = await pool.fetch(
        "SELECT id, phone, trust_score, is_active FROM tg_accounts "
        "WHERE owner_id=$1 AND is_active=TRUE ORDER BY trust_score DESC NULLS LAST LIMIT $2 OFFSET $3",
        user_id, _ACC_PAGE_SIZE + 1, offset,
    )
    has_more = len(accounts) > _ACC_PAGE_SIZE
    accounts = accounts[:_ACC_PAGE_SIZE]

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        check = "✅" if acc["id"] in selected_ids else "⬜"
        trust = f" ({acc['trust_score']:.0f}%)" if acc.get("trust_score") is not None else ""
        kb.button(
            text=f"{check} {acc['phone']}{trust}",
            callback_data=GeoPresenceCb(action="acc_tog", item=str(acc["id"])),
        )
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️", callback_data=GeoPresenceCb(action="acc_page", page=page - 1))
    if has_more:
        nav.button(text="▶️", callback_data=GeoPresenceCb(action="acc_page", page=page + 1))
    if page > 0 or has_more:
        nav.adjust(2)
        kb.attach(nav)

    action_row = InlineKeyboardBuilder()
    action_row.button(text="✅ Все", callback_data=GeoPresenceCb(action="acc_all"))
    action_row.button(text="🗑️ Сбросить", callback_data=GeoPresenceCb(action="acc_clear"))
    action_row.adjust(2)
    kb.attach(action_row)

    done_row = InlineKeyboardBuilder()
    sel_count = len(selected_ids)
    done_text = f"➡️ Далее ({sel_count} акк.)" if sel_count else "➡️ Далее"
    done_row.button(text=done_text, callback_data=GeoPresenceCb(action="acc_done"))
    done_row.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_geo"))
    done_row.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    done_row.adjust(1)
    kb.attach(done_row)

    geo_preset = sd.get("geo_preset", "—")
    geo_label = GEO_PRESETS.get(geo_preset, {}).get("label", geo_preset)
    geo_list = sd.get("geo_list") or []
    n_cities = len(geo_list)

    text = (
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 6/8 — Аккаунты</b>\n"
        f"Выберите аккаунты для создания каналов.\n"
        f"Будет использован round-robin.\n\n"
        f"📍 Гео: {geo_label} ({n_cities} городов)\n"
        f"Выбрано аккаунтов: {sel_count}\n\n"
        f"Нажмите на аккаунт чтобы выбрать/снять:"
    )

    if send_new and original_message:
        await original_message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")
    elif hasattr(callback, "message") and callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup(), parse_mode="HTML")


@router.callback_query(GeoPresenceCb.filter(F.action == "acc_page"), GlobalPresenceFSM.choosing_accounts)
async def cb_gp_acc_page(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await _show_accounts_step(callback, state, pool, page=callback_data.page)


@router.callback_query(GeoPresenceCb.filter(F.action == "acc_tog"), GlobalPresenceFSM.choosing_accounts)
async def cb_gp_acc_toggle(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    acc_id = int(callback_data.item or 0)
    sd = await state.get_data()
    selected: list[int] = list(sd.get("selected_acc_ids") or [])
    if acc_id in selected:
        selected.remove(acc_id)
    else:
        selected.append(acc_id)
    await state.update_data(selected_acc_ids=selected)
    await _show_accounts_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "acc_all"), GlobalPresenceFSM.choosing_accounts)
async def cb_gp_acc_all(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    all_accs = await pool.fetch(
        "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
        callback.from_user.id,
    )
    await state.update_data(selected_acc_ids=[a["id"] for a in all_accs])
    await _show_accounts_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "acc_clear"), GlobalPresenceFSM.choosing_accounts)
async def cb_gp_acc_clear(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.update_data(selected_acc_ids=[])
    await _show_accounts_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "acc_done"), GlobalPresenceFSM.choosing_accounts)
async def cb_gp_acc_done(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    sd = await state.get_data()
    selected_ids: list[int] = sd.get("selected_acc_ids") or []
    if not selected_ids:
        await callback.answer("⚠️ Выберите хотя бы один аккаунт!", show_alert=True)
        return
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.previewing)
    await _show_preview(callback, state, pool)


# ── Step 7: Preview ────────────────────────────────────────────────────────

async def _show_preview(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    sd = await state.get_data()
    asset_type = sd.get("asset_type", "channel")
    name_pattern = sd.get("name_pattern", "")
    username_pattern = sd.get("username_pattern")
    geo_preset = sd.get("geo_preset", "")
    geo_list: list[dict] = sd.get("geo_list") or []
    selected_acc_ids: list[int] = sd.get("selected_acc_ids") or []
    template_name = sd.get("template_name") or "Нет"

    # Load account phones for display
    if selected_acc_ids:
        acc_rows = await pool.fetch(
            "SELECT phone FROM tg_accounts WHERE id = ANY($1)", selected_acc_ids
        )
        acc_phones = [r["phone"] for r in acc_rows]
    else:
        acc_phones = []

    geo_label = GEO_PRESETS.get(geo_preset, {}).get("label", geo_preset or "Кастомный список")
    n_cities = len(geo_list)
    n_countries = len({g.get("country_code") for g in geo_list if g.get("country_code")})
    estimated = estimate_duration_minutes(n_cities)

    # Sample preview (first 3 cities)
    sample = geo_list[:3]
    preview_lines = []
    for i, geo in enumerate(sample):
        name = render_pattern(name_pattern, {**geo, "index": i + 1})
        if username_pattern:
            uname = "@" + slugify(render_pattern(username_pattern, {**geo, "index": i + 1}))[:32]
        else:
            uname = "(без username)"
        preview_lines.append(f"  📡 {name} → <code>{uname}</code>")
    preview_text = "\n".join(preview_lines)
    if n_cities > 3:
        preview_text += f"\n  … и ещё {n_cities - 3} городов"

    accs_text = ", ".join(acc_phones[:5])
    if len(acc_phones) > 5:
        accs_text += f" (+{len(acc_phones) - 5})"

    asset_emoji = {"channel": "📡", "group": "👥", "bot": "🤖"}.get(asset_type, "📦")
    hours = estimated // 60
    mins = estimated % 60
    duration_str = f"{hours}ч {mins}м" if hours else f"{mins}м"

    text = (
        f"🌍 <b>Global Presence Plan — Предпросмотр</b>\n"
        f"{'─' * 28}\n"
        f"{asset_emoji} Тип: Каналы\n"
        f"📍 Гео: {geo_label}\n"
        f"🗺️ Охват: {n_countries} стран / {n_cities} городов\n"
        f"📋 Шаблон: {template_name}\n"
        f"🔤 Название: <code>{name_pattern}</code>\n"
        f"🔗 Username: <code>{username_pattern or '—'}</code>\n"
        f"👤 Аккаунты: {accs_text} (round-robin)\n"
        f"⏱️ Длительность: ~{duration_str} (safe mode)\n\n"
        f"Примеры:\n{preview_text}\n\n"
        f"⚠️ Это создаст <b>{n_cities} каналов</b> в Telegram."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=GeoPresenceCb(action="confirm"))
    kb.button(text="✏️ Изменить гео", callback_data=GeoPresenceCb(action="back_to_geo"))
    kb.button(text="📋 Изменить шаблон", callback_data=GeoPresenceCb(action="back_to_tpl"))
    kb.button(text="👤 Изменить аккаунты", callback_data=GeoPresenceCb(action="back_to_acc"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(GeoPresenceCb.filter(F.action == "confirm"), GlobalPresenceFSM.previewing)
async def cb_gp_confirm_preview(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.confirming)

    sd = await state.get_data()
    n_cities = len(sd.get("geo_list") or [])
    warning = ""
    if n_cities > 20:
        warning = f"\n\n⚠️ <b>Внимание:</b> Вы создаёте {n_cities} каналов. Это займёт значительное время. Убедитесь что у вас достаточно аккаунтов."

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=GeoPresenceCb(action="launch"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_preview"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    await _edit(
        callback,
        f"🌍 <b>Финальное подтверждение</b>\n\n"
        f"Это создаст Telegram-инфраструктуру в <b>{n_cities} городах</b>.\n"
        f"Операция будет запущена через очередь — вы получите уведомление о завершении.{warning}\n\n"
        f"<b>Запустить?</b>",
        markup=kb.as_markup(),
    )


# ── Step 8: Launch ─────────────────────────────────────────────────────────

@router.callback_query(GeoPresenceCb.filter(F.action == "launch"), GlobalPresenceFSM.confirming)
async def cb_gp_launch(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer("⏳ Создаём план…")
    sd = await state.get_data()

    asset_type = sd.get("asset_type", "channel")
    name_pattern = sd.get("name_pattern", "Channel {{CITY}}")
    username_pattern = sd.get("username_pattern")
    geo_list: list[dict] = sd.get("geo_list") or []
    selected_acc_ids: list[int] = sd.get("selected_acc_ids") or []
    template_id = sd.get("template_id")
    geo_preset = sd.get("geo_preset", "")

    if not geo_list or not selected_acc_ids:
        await _edit(callback, "❌ Недостаточно данных для запуска. Начните сначала.")
        await state.clear()
        return

    # Build targets
    targets = build_targets(geo_list, asset_type, name_pattern, username_pattern, selected_acc_ids)

    # Create plan in DB
    plan_id = await db.create_global_presence_plan(
        pool,
        owner_id=callback.from_user.id,
        asset_type=asset_type,
        name_pattern=name_pattern,
        username_pattern=username_pattern,
        geo_selection={"preset": geo_preset, "count": len(geo_list)},
        account_selection={"account_ids": selected_acc_ids},
        template_id=template_id,
    )

    # Insert targets
    await db.create_global_presence_targets(pool, plan_id, targets)

    # Queue the operation
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items) "
        "VALUES($1,'global_presence_channel','pending',$2::jsonb,$3) RETURNING id",
        callback.from_user.id,
        json.dumps({"plan_id": plan_id}),
        len(targets),
    )

    # Link operation to plan
    await db.link_plan_to_operation(pool, plan_id, op_id)

    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Прогресс", callback_data=GeoPresenceCb(action="progress", plan_id=plan_id))
    kb.button(text="◀️ Меню", callback_data=GeoPresenceCb(action="plans_list"))
    kb.adjust(1)

    await _edit(
        callback,
        f"✅ <b>Global Presence Plan #{plan_id} запущен!</b>\n\n"
        f"📡 Каналов для создания: {len(targets)}\n"
        f"🔢 Операция в очереди: #{op_id}\n\n"
        f"Вы получите уведомление по завершении.\n"
        f"Нажмите «Прогресс» для отслеживания.",
        markup=kb.as_markup(),
    )


# ── Progress & Report ──────────────────────────────────────────────────────

@router.callback_query(GeoPresenceCb.filter(F.action == "progress"))
async def cb_gp_progress(
    callback: CallbackQuery, callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    plan_id = callback_data.plan_id
    if not plan_id:
        await callback.answer("Укажите ID плана", show_alert=True)
        return

    plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return

    stats = await db.get_global_presence_stats(pool, plan_id)
    op_id = plan.get("op_id")

    op_status = "—"
    if op_id:
        op_row = await pool.fetchrow("SELECT status, done_items, total_items FROM operation_queue WHERE id=$1", op_id)
        if op_row:
            op_status = op_row["status"]

    total = stats["total"]
    done = stats["done"]
    failed = stats["failed"]
    pending = stats["pending"]

    pct = int(done / total * 100) if total else 0
    bar_filled = pct // 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    # Find currently running city
    current_row = await pool.fetchrow(
        "SELECT city FROM global_presence_targets WHERE plan_id=$1 AND status='running' LIMIT 1",
        plan_id,
    )
    current_city = current_row["city"] if current_row else "—"

    # Estimate remaining
    remaining = pending
    estimated_remaining = estimate_duration_minutes(remaining)
    hours = estimated_remaining // 60
    mins = estimated_remaining % 60
    remaining_str = f"~{hours}ч {mins}м" if hours else f"~{mins}м"

    status_map = {"queued": "В очереди", "running": "Выполняется", "done": "Завершён",
                  "failed": "Ошибка", "cancelled": "Отменён", "draft": "Черновик"}

    text = (
        f"🌍 <b>Global Presence Plan #{plan_id}</b>\n"
        f"Статус: {status_map.get(plan['status'], plan['status'])}\n"
        f"{'─' * 28}\n"
        f"Всего: {total}\n"
        f"✅ Создано: {done}\n"
        f"❌ Ошибок: {failed}\n"
        f"⏳ Ожидают: {pending}\n"
        f"⚡ Текущий: {current_city}\n\n"
        f"Прогресс: {bar} {pct}%\n"
        f"Осталось: {remaining_str}\n"
        f"Операция: #{op_id or '—'} ({op_status})"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=GeoPresenceCb(action="progress", plan_id=plan_id))
    if failed > 0:
        kb.button(text="🔁 Повторить ошибки", callback_data=GeoPresenceCb(action="retry", plan_id=plan_id))
    kb.button(text="📋 Отчёт", callback_data=GeoPresenceCb(action="report", plan_id=plan_id))
    kb.button(text="◀️ Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.adjust(2)

    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(GeoPresenceCb.filter(F.action == "retry"))
async def cb_gp_retry(
    callback: CallbackQuery, callback_data: GeoPresenceCb, pool: asyncpg.Pool,
) -> None:
    plan_id = callback_data.plan_id
    plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return

    reset_count = await db.reset_failed_targets(pool, plan_id)
    if reset_count == 0:
        await callback.answer("Нет повторяемых ошибок", show_alert=True)
        return

    # Queue new retry operation
    op_id = await pool.fetchval(
        "INSERT INTO operation_queue(owner_id, op_type, status, params, total_items) "
        "VALUES($1,'global_presence_channel','pending',$2::jsonb,$3) RETURNING id",
        callback.from_user.id,
        json.dumps({"plan_id": plan_id}),
        reset_count,
    )
    await db.link_plan_to_operation(pool, plan_id, op_id)
    await callback.answer(f"✅ {reset_count} целей поставлено в очередь на повтор (op #{op_id})", show_alert=True)


@router.callback_query(GeoPresenceCb.filter(F.action == "report"))
async def cb_gp_report(
    callback: CallbackQuery, callback_data: GeoPresenceCb, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    plan_id = callback_data.plan_id
    plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return

    stats = await db.get_global_presence_stats(pool, plan_id)

    # Get created channels
    done_targets = await pool.fetch(
        "SELECT city, planned_name, planned_username, result_asset_id "
        "FROM global_presence_targets WHERE plan_id=$1 AND status='done' ORDER BY id LIMIT 20",
        plan_id,
    )
    failed_targets = await pool.fetch(
        "SELECT city, planned_name, error_message "
        "FROM global_presence_targets WHERE plan_id=$1 AND status='failed' LIMIT 10",
        plan_id,
    )

    done_lines = "\n".join(
        f"  ✅ {t['city'] or '?'}: {t['planned_name'] or '?'}"
        + (f" (id:{t['result_asset_id']})" if t["result_asset_id"] else "")
        for t in done_targets[:10]
    )
    if stats["done"] > 10:
        done_lines += f"\n  … и ещё {stats['done'] - 10}"

    fail_lines = "\n".join(
        f"  ❌ {t['city'] or '?'}: {(t['error_message'] or '?')[:60]}"
        for t in failed_targets[:5]
    )

    text = (
        f"📊 <b>Отчёт: Global Presence Plan #{plan_id}</b>\n"
        f"{'─' * 28}\n"
        f"Паттерн: <code>{plan['name_pattern']}</code>\n"
        f"Статус: {plan['status']}\n\n"
        f"📊 Итого: {stats['total']}\n"
        f"✅ Создано: {stats['done']}\n"
        f"❌ Ошибок: {stats['failed']}\n"
        f"⏳ Ожидают: {stats['pending']}\n\n"
        + (f"Созданные каналы:\n{done_lines}\n\n" if done_lines else "")
        + (f"Ошибки:\n{fail_lines}" if fail_lines else "")
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Прогресс", callback_data=GeoPresenceCb(action="progress", plan_id=plan_id))
    kb.adjust(1)
    await _edit(callback, text, markup=kb.as_markup())


# ── Plans List ─────────────────────────────────────────────────────────────

@router.callback_query(GeoPresenceCb.filter(F.action == "plans_list"))
async def cb_gp_plans_list(
    callback: CallbackQuery, callback_data: GeoPresenceCb, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    plans = await db.get_global_presence_plans(pool, callback.from_user.id, limit=8)
    if not plans:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Создать план", callback_data=GeoPresenceCb(action="menu"))
        await _edit(
            callback,
            "🌍 <b>Global Presence Factory</b>\n\n"
            "У вас ещё нет планов присутствия.\n"
            "Нажмите «Создать план» чтобы начать.",
            markup=kb.as_markup(),
        )
        return

    status_emoji = {"queued": "⏳", "running": "⚡", "done": "✅", "failed": "❌",
                    "cancelled": "🚫", "draft": "📝"}
    kb = InlineKeyboardBuilder()
    for plan in plans:
        emoji = status_emoji.get(plan["status"], "❓")
        import json as _json
        geo_sel = plan["geo_selection"] if isinstance(plan["geo_selection"], dict) else _json.loads(plan["geo_selection"] or "{}")
        count = geo_sel.get("count", "?")
        label = f"{emoji} #{plan['id']} — {plan['name_pattern'][:20]} ({count} городов)"
        kb.button(text=label, callback_data=GeoPresenceCb(action="progress", plan_id=plan["id"]))
    kb.button(text="➕ Новый план", callback_data=GeoPresenceCb(action="menu"))
    kb.adjust(1)

    await _edit(
        callback,
        "🌍 <b>Global Presence — Мои планы</b>\n\n"
        "Нажмите на план для просмотра прогресса:",
        markup=kb.as_markup(),
    )


# ── Navigation ─────────────────────────────────────────────────────────────

@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_geo"))
async def cb_gp_back_geo(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_uname"))
async def cb_gp_back_uname(
    callback: CallbackQuery, state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.entering_username_pattern)
    sd = await state.get_data()
    await _show_username_pattern_step(callback, state, prefill=sd.get("username_pattern"))


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_tpl"))
async def cb_gp_back_tpl(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.choosing_template)
    await _show_template_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_acc"))
async def cb_gp_back_acc(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.choosing_accounts)
    await _show_accounts_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_preview"))
async def cb_gp_back_preview(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.previewing)
    await _show_preview(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "cancel"))
async def cb_gp_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    from bot.callbacks import BmCb
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Операции", callback_data=BmCb(action="operations"))
    kb.button(text="🌍 Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.adjust(2)
    await _edit(callback, "❌ <b>Global Presence Factory</b> — отменено.", markup=kb.as_markup())

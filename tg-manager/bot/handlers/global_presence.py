"""Global Presence Factory — guided FSM wizard for worldwide Telegram channel creation."""

from __future__ import annotations

import logging

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
from services.presence_planner import (
    render_pattern,
    build_targets,
    estimate_duration_minutes,
)
from services.username_engine import slugify
from services.logger import log_exc_swallow
from services import operation_bus, infra_orchestrator, intelligence_engine

log = logging.getLogger(__name__)
router = Router()

_TPL_PAGE_SIZE = 5
_ACC_PAGE_SIZE = 8


# ── Helpers ────────────────────────────────────────────────────────────────


def _back_cancel_row() -> list:
    """Deprecated stub — use _back_cancel_kb() instead."""
    return []


async def _edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.answer()
    except Exception:
        log.debug(
            "global_presence: callback answer already sent or expired", exc_info=True
        )
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
    kb.button(
        text="◀️ Назад", callback_data=GeoPresenceCb(action=back_action, plan_id=plan_id)
    )
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2)
    return kb.as_markup()


# ── Step 1: Entry / Asset Type ─────────────────────────────────────────────


@router.callback_query(GeoPresenceCb.filter(F.action == "menu"))
async def cb_gp_menu(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Global Presence Factory", "enterprise"),
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    await callback.answer()
    await state.clear()

    # Show active/recent plans count
    try:
        recent_plans = await db.get_global_presence_plans(
            pool, callback.from_user.id, limit=3
        )
    except Exception:
        log_exc_swallow(log, "cb_gp_menu: get_global_presence_plans failed")
        recent_plans = []
    running_count = sum(1 for p in recent_plans if p["status"] in ("running", "queued"))
    plans_hint = ""
    if running_count:
        plans_hint = f"\n⚡ <b>Активных операций: {running_count}</b> — <a href='tg://callback'>Мои планы ↓</a>\n"
    elif recent_plans:
        last = recent_plans[0]
        status_map = {
            "done": "✅ завершён",
            "failed": "❌ ошибка",
            "cancelled": "🚫 отменён",
            "queued": "⏳ в очереди",
        }
        last_status = status_map.get(last["status"], last["status"])
        plans_hint = f"\n📋 Последний план #{last['id']}: {last_status}\n"

    await state.set_state(GlobalPresenceFSM.choosing_asset_type)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="📡 Каналы", callback_data=GeoPresenceCb(action="asset", item="channel")
    )
    kb.button(
        text="👥 Группы", callback_data=GeoPresenceCb(action="asset", item="group")
    )
    kb.button(
        text="🤖 Боты (BotFather)",
        callback_data=GeoPresenceCb(action="asset", item="bot"),
    )
    kb.button(
        text="📦 Пакет (Канал+Группа)",
        callback_data=GeoPresenceCb(action="asset", item="package"),
    )
    kb.button(
        text="🌐 Полный пакет (К+Г+Б)",
        callback_data=GeoPresenceCb(action="asset", item="full_package"),
    )
    if recent_plans:
        kb.button(text="📋 Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2, 1, 1, 1, 1)
    await callback.message.edit_text(
        f"🌍 <b>Global Presence Factory</b>\n"
        f"{'─' * 28}\n"
        f"Создайте Telegram-инфраструктуру сразу в сотнях городов — "
        f"каналы, группы и боты с локализованными названиями и username.\n"
        f"{plans_hint}\n"
        f"<b>Шаг 1/8 — Выберите тип актива:</b>\n"
        f"📡 <b>Каналы</b> — публичные каналы под каждый город\n"
        f"👥 <b>Группы</b> — супергруппы для обсуждений\n"
        f"🤖 <b>Боты</b> — боты через BotFather (нужны аккаунты)\n"
        f"📦 <b>Пакет</b> — канал <i>и</i> группа на каждый город\n"
        f"🌐 <b>Полный пакет</b> — канал + группа + бот на каждый город",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "asset"), GlobalPresenceFSM.choosing_asset_type
)
async def cb_gp_asset(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    asset = callback_data.item or "channel"
    if asset not in ("channel", "group", "bot", "package", "full_package"):
        await callback.answer("Неподдерживаемый тип", show_alert=True)
        return
    await callback.answer()
    await state.update_data(asset_type=asset)
    await state.set_state(GlobalPresenceFSM.choosing_template)
    await _show_template_step(callback, state, pool, asset_type=asset, page=0)


# ── Step 2: Template ────────────────────────────────────────────────────────


async def _show_template_step(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
    asset_type: str = "channel",
    page: int = 0,
) -> None:
    from services.preset_templates import get_presets

    user_id = callback.from_user.id
    offset = page * _TPL_PAGE_SIZE
    templates = await pool.fetch(
        "SELECT id, name FROM asset_templates WHERE owner_id=$1 AND asset_type=$2 "
        "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
        user_id,
        asset_type,
        _TPL_PAGE_SIZE + 1,
        offset,
    )
    has_more = len(templates) > _TPL_PAGE_SIZE
    templates = templates[:_TPL_PAGE_SIZE]

    kb = InlineKeyboardBuilder()

    # Library presets on first page (top-3)
    lib_count = 0
    if page == 0:
        lib_atype = (
            asset_type if asset_type not in ("package", "full_package") else "channel"
        )
        lib_presets = get_presets(lib_atype)[:3]
        for p in lib_presets:
            kb.button(
                text=f"📚 {p['name'][:28]}",
                callback_data=GeoPresenceCb(
                    action="sel_tpl", item=f"lib__{lib_atype}__{p['id']}"
                ),
            )
        lib_count = len(lib_presets)

    for tpl in templates:
        kb.button(
            text=f"📋 {tpl['name'][:30]}",
            callback_data=GeoPresenceCb(action="sel_tpl", item=str(tpl["id"])),
        )
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(
            text="◀️", callback_data=GeoPresenceCb(action="tpl_page", page=page - 1)
        )
    if has_more:
        nav.button(
            text="▶️", callback_data=GeoPresenceCb(action="tpl_page", page=page + 1)
        )
    if page > 0 or has_more:
        nav.adjust(2)
        kb.attach(nav)

    kb.button(text="⏭️ Без шаблона", callback_data=GeoPresenceCb(action="skip_tpl"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="menu"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    user_tpl_count = len(templates) + offset
    header_parts = []
    if lib_count and page == 0:
        header_parts.append(f"📚 Готовых в библиотеке: {lib_count}")
    if user_tpl_count > 0:
        header_parts.append(
            f"📋 Ваших шаблонов: {user_tpl_count}{'+' if has_more else ''}"
        )
    elif page == 0 and not lib_count:
        header_parts.append(
            "📋 Ваших шаблонов: 0 (создайте в /menu → ⚙️ Настройки → 📄 Шаблоны)"
        )
    header = (
        "\n".join(header_parts)
        if header_parts
        else "📚 Доступны готовые шаблоны из библиотеки"
    )

    _asset_label_map = {
        "channel": "канала",
        "group": "группы",
        "bot": "бота",
        "package": "канала/группы",
        "full_package": "канала/группы/бота",
    }
    asset_label = _asset_label_map.get(asset_type, "актива")
    await _edit(
        callback,
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 2/8 — Шаблон {asset_label}</b>\n"
        f"Шаблон задаёт описание, аватар и первый пост.\n\n"
        f"{header}",
        markup=kb.as_markup(),
    )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "tpl_page"), GlobalPresenceFSM.choosing_template
)
async def cb_gp_tpl_page(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    asset_type = sd.get("asset_type", "channel")
    await _show_template_step(
        callback, state, pool, asset_type=asset_type, page=callback_data.page
    )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "sel_tpl"), GlobalPresenceFSM.choosing_template
)
async def cb_gp_sel_tpl(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    item = callback_data.item or ""

    if item.startswith("lib__"):
        # Library preset: item = "lib__<atype>__<preset_id>"
        from services.preset_templates import get_preset

        parts = item.split("__", 2)
        if len(parts) != 3:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        lib_atype, preset_id = parts[1], parts[2]
        preset = get_preset(lib_atype, preset_id)
        if not preset:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        await callback.answer()
        import json as _json

        await state.update_data(
            template_id=None,
            template_name=preset["name"],
            template_data=_json.dumps(preset["template"]),
        )
    else:
        tpl_id = int(item) if item.isdigit() else 0
        tpl = await pool.fetchrow(
            "SELECT id, name, template FROM asset_templates WHERE id=$1 AND owner_id=$2",
            tpl_id,
            callback.from_user.id,
        )
        if not tpl:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        await callback.answer()
        await state.update_data(template_id=tpl_id, template_name=tpl["name"])

    await state.set_state(GlobalPresenceFSM.entering_name_pattern)
    await _show_name_pattern_step(callback, state, prefill=None)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "skip_tpl"), GlobalPresenceFSM.choosing_template
)
async def cb_gp_skip_tpl(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.update_data(template_id=None, template_name=None)
    await state.set_state(GlobalPresenceFSM.entering_name_pattern)
    await _show_name_pattern_step(callback, state, prefill=None)


# ── Step 3: Name Pattern ───────────────────────────────────────────────────


async def _show_name_pattern_step(
    callback: CallbackQuery, state: FSMContext, prefill: str | None
) -> None:
    sd = await state.get_data()
    asset_type = sd.get("asset_type", "channel")
    _asset_noun = {
        "channel": "канала",
        "group": "группы",
        "bot": "бота",
        "package": "канала/группы",
        "full_package": "канала/группы/бота",
    }
    asset_noun = _asset_noun.get(asset_type, "актива")
    examples = [
        "Crypto News {{CITY}}",
        "AI Jobs {{CITY}}",
        "{{CITY}} Business Hub",
        "Trading {{COUNTRY_CODE}} {{CITY}}",
    ]
    ex_text = "\n".join(f"  • <code>{e}</code>" for e in examples)
    bot_note = (
        "\n\n💡 <i>Для ботов: название — отображаемое имя в Telegram (не username).</i>"
        if asset_type in ("bot", "full_package")
        else ""
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_tpl"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2)
    await _edit(
        callback,
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 3/8 — Паттерн названия</b>\n"
        f"Введите шаблон для названия {asset_noun}.\n\n"
        f"Доступные плейсхолдеры:\n"
        f"  <code>{{{{CITY}}}}</code> — город (English)\n"
        f"  <code>{{{{CITY_NAME}}}}</code> — название на языке страны (Москва, Київ, Wien…)\n"
        f"  <code>{{{{COUNTRY}}}}</code> — страна\n"
        f"  <code>{{{{COUNTRY_CODE}}}}</code> — код страны (DE, FR…)\n"
        f"  <code>{{{{CITY_SLUG}}}}</code> — транслит-слаг города (для username)\n"
        f"  <code>{{{{INDEX}}}}</code> — порядковый номер\n\n"
        f"Примеры:\n{ex_text}"
        + bot_note
        + "\n\n"
        + (f"💡 Последний ввод: <code>{prefill}</code>\n\n" if prefill else "")
        + "Введите паттерн:",
        markup=kb.as_markup(),
    )


@router.message(GlobalPresenceFSM.entering_name_pattern)
async def msg_gp_name_pattern(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    pattern = (message.text or "").strip()
    if not pattern:
        await _reply(
            message, "⚠️ Паттерн не может быть пустым. Введите снова:", _cancel_kb()
        )
        return
    if len(pattern) > 200:
        await _reply(
            message,
            "⚠️ Слишком длинный паттерн (макс. 200 символов). Попробуйте короче:",
            _cancel_kb(),
        )
        return

    # Show examples before confirming
    sample_geos = [
        {
            "city": "Berlin",
            "city_slug": "berlin",
            "country": "Germany",
            "country_code": "de",
            "index": 1,
        },
        {
            "city": "Paris",
            "city_slug": "paris",
            "country": "France",
            "country_code": "fr",
            "index": 2,
        },
        {
            "city": "Madrid",
            "city_slug": "madrid",
            "country": "Spain",
            "country_code": "es",
            "index": 3,
        },
    ]
    examples_text = "\n".join(
        f"  📡 <b>{render_pattern(pattern, g)}</b>" for g in sample_geos
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


@router.callback_query(
    GeoPresenceCb.filter(F.action == "accept_name"),
    GlobalPresenceFSM.entering_name_pattern,
)
async def cb_gp_accept_name(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    await state.update_data(
        name_pattern=sd.get("name_pattern_pending", ""), name_pattern_pending=None
    )
    await state.set_state(GlobalPresenceFSM.entering_username_pattern)
    await _show_username_pattern_step(callback, state, prefill=None)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "retry_name"),
    GlobalPresenceFSM.entering_name_pattern,
)
async def cb_gp_retry_name(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    await _show_name_pattern_step(
        callback, state, prefill=sd.get("name_pattern_pending")
    )


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
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="retry_name"))
    kb.button(text="⏭️ Без username", callback_data=GeoPresenceCb(action="skip_uname"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2, 1)
    await _edit(
        callback,
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 4/8 — Паттерн username</b>\n"
        f"Username делает канал публичным и находимым.\n"
        f"Правила: 5–32 символа, a-z, 0-9, подчёркивание.\n\n"
        f"Примеры:\n{ex_text}\n\n"
        + (f"💡 Последний ввод: <code>{prefill}</code>\n\n" if prefill else "")
        + "Введите паттерн или пропустите:",
        markup=kb.as_markup(),
    )


@router.message(GlobalPresenceFSM.entering_username_pattern)
async def msg_gp_username_pattern(message: Message, state: FSMContext) -> None:
    pattern = (message.text or "").strip()
    if not pattern:
        await _reply(
            message, "⚠️ Введите паттерн или нажмите «Без username».", _cancel_kb()
        )
        return

    sample_geos = [
        {
            "city": "Berlin",
            "city_slug": "berlin",
            "country": "Germany",
            "country_code": "de",
            "index": 1,
        },
        {
            "city": "Paris",
            "city_slug": "paris",
            "country": "France",
            "country_code": "fr",
            "index": 2,
        },
        {
            "city": "Madrid",
            "city_slug": "madrid",
            "country": "Spain",
            "country_code": "es",
            "index": 3,
        },
    ]
    examples_text = "\n".join(
        f"  @<b>{slugify(render_pattern(pattern, g))[:32]}</b>" for g in sample_geos
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


@router.callback_query(
    GeoPresenceCb.filter(F.action == "accept_uname"),
    GlobalPresenceFSM.entering_username_pattern,
)
async def cb_gp_accept_uname(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
) -> None:
    await callback.answer()
    sd = await state.get_data()
    await state.update_data(
        username_pattern=sd.get("username_pattern_pending", ""),
        username_pattern_pending=None,
    )
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "retry_uname"),
    GlobalPresenceFSM.entering_username_pattern,
)
async def cb_gp_retry_uname(
    callback: CallbackQuery, callback_data: GeoPresenceCb, state: FSMContext
) -> None:
    await callback.answer()
    sd = await state.get_data()
    await _show_username_pattern_step(
        callback, state, prefill=sd.get("username_pattern_pending")
    )


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
    kb.button(
        text="✏️ Ввести города вручную", callback_data=GeoPresenceCb(action="geo_custom")
    )
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


@router.callback_query(
    GeoPresenceCb.filter(F.action == "geo"), GlobalPresenceFSM.choosing_geo
)
async def cb_gp_geo_preset(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
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


@router.callback_query(
    GeoPresenceCb.filter(F.action == "geo_custom"), GlobalPresenceFSM.choosing_geo
)
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
        "<b>Шаг 5/8 — Кастомные города</b>\n\n"
        "Введите города, по одному на строку:\n"
        "<code>Berlin\nParis\nMadrid\nTokyo</code>\n\n"
        "Или с деталями через запятую:\n"
        "<code>Berlin, Germany, de</code>\n\n"
        "📎 <b>Или загрузите CSV-файл</b> с городами.\n"
        "Формат: <code>city, country, country_code</code> (первые 3 колонки).",
        markup=kb.as_markup(),
    )


async def _parse_geo_from_text_or_file(text: str) -> list[dict]:
    """Parse geo list from plain text (city per line or CSV format)."""
    return parse_custom_geo_list(text)


async def _parse_geo_csv_bytes(raw: bytes) -> list[dict] | None:
    """Parse CSV bytes → list of geo dicts. Returns None on decode error."""
    import csv
    import io

    for enc in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None

    # Try to detect delimiter
    sample = text[:2000]
    delimiter = "," if sample.count(",") >= sample.count(";") else ";"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    lines: list[str] = []
    for row in reader:
        if not row:
            continue
        # Skip header rows
        first = row[0].strip().lower()
        if first in ("city", "город", "name", "название", "#", ""):
            continue
        # Rebuild as comma-separated for parse_custom_geo_list
        lines.append(", ".join(c.strip() for c in row[:3] if c.strip()))
    return parse_custom_geo_list("\n".join(lines)) if lines else None


@router.message(GlobalPresenceFSM.entering_custom_geo, F.document)
async def msg_gp_custom_geo_file(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    """Handle CSV / TXT file upload for city list."""
    doc = message.document
    if not doc:
        await _reply(message, "⚠️ Документ не получен.", _cancel_kb())
        return
    filename = (doc.file_name or "").lower()
    if not (filename.endswith(".csv") or filename.endswith(".txt")):
        await _reply(
            message, "⚠️ Поддерживаются только .csv и .txt файлы.", _cancel_kb()
        )
        return
    if doc.file_size and doc.file_size > 512_000:
        await _reply(message, "⚠️ Файл слишком большой (максимум 512 КБ).", _cancel_kb())
        return

    wait_msg = await message.answer("⏳ Читаю файл…")
    try:
        file = await message.bot.get_file(doc.file_id)
        raw = await message.bot.download_file(file.file_path)
        content = raw.read() if hasattr(raw, "read") else bytes(raw)
    except Exception as e:
        await wait_msg.delete()
        await _reply(message, f"⚠️ Не удалось скачать файл: {e}", _cancel_kb())
        return

    await wait_msg.delete()

    if filename.endswith(".csv"):
        geo_list = await _parse_geo_csv_bytes(content)
    else:
        try:
            text = content.decode("utf-8-sig", errors="replace")
        except Exception:
            text = content.decode("latin-1", errors="replace")
        geo_list = parse_custom_geo_list(text)

    if not geo_list:
        await _reply(
            message,
            "⚠️ Не удалось распознать города из файла.\n\n"
            "Ожидаемый формат (одна строка = один город):\n"
            "<code>Berlin, Germany, de\nParis, France, fr</code>",
            _cancel_kb(),
        )
        return

    await state.update_data(geo_preset="custom", geo_list=geo_list)
    await state.set_state(GlobalPresenceFSM.choosing_accounts)

    await message.answer(
        f"✅ <b>Загружено {len(geo_list)} городов из файла</b>\n"
        f"Первые 5: {', '.join(g['city'] for g in geo_list[:5])}{'…' if len(geo_list) > 5 else ''}",
        parse_mode="HTML",
    )

    _msg = message

    class FakeCallback:
        from_user = _msg.from_user
        message = _msg

        async def answer(self, *a, **kw):
            pass

    await _show_accounts_step(
        FakeCallback(), state, pool, page=0, send_new=True, original_message=message
    )


@router.message(GlobalPresenceFSM.entering_custom_geo)
async def msg_gp_custom_geo(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await _reply(message, "⚠️ Введите хотя бы один город.", _cancel_kb())
        return
    geo_list = parse_custom_geo_list(text)
    if not geo_list:
        await _reply(
            message,
            "⚠️ Не удалось распознать ни одного города. Введите снова.",
            _cancel_kb(),
        )
        return
    await state.update_data(geo_preset="custom", geo_list=geo_list)
    await state.set_state(GlobalPresenceFSM.choosing_accounts)

    _msg = message

    class FakeCallback:
        from_user = _msg.from_user
        message = _msg

        async def answer(self, *a, **kw):
            pass

    await _show_accounts_step(
        FakeCallback(), state, pool, page=0, send_new=True, original_message=message
    )


# ── Step 6: Account Selection ──────────────────────────────────────────────


async def _show_accounts_step(
    callback,
    state: FSMContext,
    pool: asyncpg.Pool,
    page: int = 0,
    send_new: bool = False,
    original_message: Message | None = None,
) -> None:
    user_id = callback.from_user.id
    sd = await state.get_data()
    selected_ids: list[int] = sd.get("selected_acc_ids") or []

    offset = page * _ACC_PAGE_SIZE
    try:
        accounts = await pool.fetch(
            "SELECT id, phone, trust_score, is_active FROM tg_accounts "
            "WHERE owner_id=$1 AND is_active=TRUE ORDER BY trust_score DESC NULLS LAST LIMIT $2 OFFSET $3",
            user_id,
            _ACC_PAGE_SIZE + 1,
            offset,
        )
    except Exception:
        log_exc_swallow(log, "_show_accounts_step: pool.fetch failed")
        accounts = []
    has_more = len(accounts) > _ACC_PAGE_SIZE
    accounts = accounts[:_ACC_PAGE_SIZE]

    geo_preset = sd.get("geo_preset", "—")
    geo_label = GEO_PRESETS.get(geo_preset, {}).get("label", geo_preset)
    geo_list = sd.get("geo_list") or []
    n_cities = len(geo_list)

    # No accounts — show a clear message with instructions
    if not accounts and page == 0:
        no_acc_kb = InlineKeyboardBuilder()
        no_acc_kb.button(
            text="◀️ Назад к гео", callback_data=GeoPresenceCb(action="back_to_geo")
        )
        no_acc_kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
        no_acc_kb.adjust(1)
        no_acc_text = (
            f"🌍 <b>Global Presence Factory</b>\n\n"
            f"<b>Шаг 6/8 — Аккаунты</b>\n\n"
            f"⚠️ <b>У вас нет активных аккаунтов</b>\n\n"
            f"Для запуска Global Presence необходимо добавить хотя бы один аккаунт "
            f"в разделе <b>/menu → 📱 Активы → 📱 Аккаунты</b>.\n\n"
            f"📍 Гео: {geo_label} ({n_cities} городов) — настроено\n\n"
            f"Добавьте аккаунт и вернитесь сюда."
        )
        if send_new and original_message:
            await original_message.answer(
                no_acc_text, reply_markup=no_acc_kb.as_markup(), parse_mode="HTML"
            )
        elif hasattr(callback, "message") and callback.message:
            try:
                await callback.message.edit_text(
                    no_acc_text, reply_markup=no_acc_kb.as_markup(), parse_mode="HTML"
                )
            except Exception:
                await callback.message.answer(
                    no_acc_text, reply_markup=no_acc_kb.as_markup(), parse_mode="HTML"
                )
        return

    kb = InlineKeyboardBuilder()
    for acc in accounts:
        check = "✅" if acc["id"] in selected_ids else "⬜"
        trust = (
            f" ({acc['trust_score']:.0f}%)"
            if acc.get("trust_score") is not None
            else ""
        )
        kb.button(
            text=f"{check} {acc['phone']}{trust}",
            callback_data=GeoPresenceCb(action="acc_tog", item=str(acc["id"])),
        )
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(
            text="◀️", callback_data=GeoPresenceCb(action="acc_page", page=page - 1)
        )
    if has_more:
        nav.button(
            text="▶️", callback_data=GeoPresenceCb(action="acc_page", page=page + 1)
        )
    if page > 0 or has_more:
        nav.adjust(2)
        kb.attach(nav)

    action_row = InlineKeyboardBuilder()
    action_row.button(text="✅ Все", callback_data=GeoPresenceCb(action="acc_all"))
    action_row.button(
        text="🗑️ Сбросить", callback_data=GeoPresenceCb(action="acc_clear")
    )
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
        await original_message.answer(
            text, reply_markup=kb.as_markup(), parse_mode="HTML"
        )
    elif hasattr(callback, "message") and callback.message:
        try:
            await callback.message.edit_text(
                text, reply_markup=kb.as_markup(), parse_mode="HTML"
            )
        except Exception:
            await callback.message.answer(
                text, reply_markup=kb.as_markup(), parse_mode="HTML"
            )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "acc_page"), GlobalPresenceFSM.choosing_accounts
)
async def cb_gp_acc_page(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await _show_accounts_step(callback, state, pool, page=callback_data.page)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "acc_tog"), GlobalPresenceFSM.choosing_accounts
)
async def cb_gp_acc_toggle(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
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


@router.callback_query(
    GeoPresenceCb.filter(F.action == "acc_all"), GlobalPresenceFSM.choosing_accounts
)
async def cb_gp_acc_all(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    try:
        all_accs = await pool.fetch(
            "SELECT id FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE",
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "cb_gp_acc_all: pool.fetch failed")
        all_accs = []
    await state.update_data(selected_acc_ids=[a["id"] for a in all_accs])
    await _show_accounts_step(callback, state, pool)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "acc_clear"), GlobalPresenceFSM.choosing_accounts
)
async def cb_gp_acc_clear(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.update_data(selected_acc_ids=[])
    await _show_accounts_step(callback, state, pool)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "acc_done"), GlobalPresenceFSM.choosing_accounts
)
async def cb_gp_acc_done(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
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


async def _show_preview(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
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
        try:
            acc_rows = await pool.fetch(
                "SELECT phone FROM tg_accounts WHERE id = ANY($1)", selected_acc_ids
            )
        except Exception:
            log_exc_swallow(log, "_show_preview: pool.fetch accounts failed")
            acc_rows = []
        acc_phones = [r["phone"] for r in acc_rows]
    else:
        acc_phones = []

    geo_label = GEO_PRESETS.get(geo_preset, {}).get(
        "label", geo_preset or "Кастомный список"
    )
    n_cities = len(geo_list)
    n_countries = len(
        {g.get("country_code") for g in geo_list if g.get("country_code")}
    )
    estimated = estimate_duration_minutes(n_cities)

    # Sample preview (first 3 cities)
    sample = geo_list[:3]
    preview_lines = []
    for i, geo in enumerate(sample):
        name = render_pattern(name_pattern, {**geo, "index": i + 1})
        if username_pattern:
            uname = (
                "@"
                + slugify(render_pattern(username_pattern, {**geo, "index": i + 1}))[
                    :32
                ]
            )
        else:
            uname = "(без username)"
        preview_lines.append(f"  📡 {name} → <code>{uname}</code>")
    preview_text = "\n".join(preview_lines)
    if n_cities > 3:
        preview_text += f"\n  … и ещё {n_cities - 3} городов"

    accs_text = ", ".join(acc_phones[:5])
    if len(acc_phones) > 5:
        accs_text += f" (+{len(acc_phones) - 5})"

    asset_emoji = {
        "channel": "📡",
        "group": "👥",
        "bot": "🤖",
        "package": "📦",
        "full_package": "📦",
    }.get(asset_type, "📦")
    _asset_label = {
        "channel": "Каналы",
        "group": "Группы",
        "bot": "Боты (BotFather)",
        "package": "Пакет (Канал+Группа)",
        "full_package": "Полный пакет (Канал+Группа+Бот)",
    }
    _asset_count_label = {
        "channel": "каналов",
        "group": "групп",
        "bot": "ботов",
        "package": "пакетов (×2 актива)",
        "full_package": "пакетов (×3 актива)",
    }
    asset_type_label = _asset_label.get(asset_type, asset_type.capitalize())
    count_label = _asset_count_label.get(asset_type, "активов")
    hours = estimated // 60
    mins = estimated % 60
    duration_str = f"{hours}ч {mins}м" if hours else f"{mins}м"
    bot_note = (
        "\n💡 <i>Username ботов должен заканчиваться на _bot</i>"
        if asset_type in ("bot", "full_package")
        else ""
    )
    pkg_note = (
        "\n📦 <i>Пакет создаст канал И группу для каждого города</i>"
        if asset_type == "package"
        else ""
    )
    fullpkg_note = (
        "\n📦 <i>Полный пакет создаст канал, группу И бота для каждого города</i>"
        if asset_type == "full_package"
        else ""
    )

    text = (
        f"🌍 <b>Global Presence Plan — Предпросмотр</b>\n"
        f"{'─' * 28}\n"
        f"{asset_emoji} Тип: {asset_type_label}\n"
        f"📍 Гео: {geo_label}\n"
        f"🗺️ Охват: {n_countries} стран / {n_cities} городов\n"
        f"📋 Шаблон: {template_name}\n"
        f"🔤 Название: <code>{name_pattern}</code>\n"
        f"🔗 Username: <code>{username_pattern or '—'}</code>\n"
        f"👤 Аккаунты: {accs_text} (round-robin)\n"
        f"⏱️ Длительность: ~{duration_str} (safe mode)\n\n"
        f"Примеры:\n{preview_text}\n\n"
        f"⚠️ Это создаст <b>{n_cities} {count_label}</b> в Telegram."
        + bot_note
        + pkg_note
        + fullpkg_note
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=GeoPresenceCb(action="confirm"))
    kb.button(text="✏️ Изменить гео", callback_data=GeoPresenceCb(action="back_to_geo"))
    kb.button(
        text="📋 Изменить шаблон", callback_data=GeoPresenceCb(action="back_to_tpl")
    )
    kb.button(
        text="👤 Изменить аккаунты", callback_data=GeoPresenceCb(action="back_to_acc")
    )
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(
    GeoPresenceCb.filter(F.action == "confirm"), GlobalPresenceFSM.previewing
)
async def cb_gp_confirm_preview(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.confirming)

    sd = await state.get_data()
    asset_type = sd.get("asset_type", "channel")
    name_pattern = sd.get("name_pattern", "")
    username_pattern = sd.get("username_pattern")
    geo_list: list[dict] = sd.get("geo_list") or []
    selected_acc_ids: list[int] = sd.get("selected_acc_ids") or []
    template_name = sd.get("template_name") or "Нет"
    geo_preset = sd.get("geo_preset", "")

    n_cities = len(geo_list)
    n_accs = len(selected_acc_ids)
    geo_label = GEO_PRESETS.get(geo_preset, {}).get(
        "label", geo_preset or "Кастомный список"
    )

    _asset_label = {
        "channel": "Каналы",
        "group": "Группы",
        "bot": "Боты (BotFather)",
        "package": "Пакет (Канал+Группа)",
        "full_package": "Полный пакет (Канал+Группа+Бот)",
    }
    _asset_count_label = {
        "channel": "каналов",
        "group": "групп",
        "bot": "ботов",
        "package": "пакетов (×2 актива)",
        "full_package": "пакетов (×3 актива)",
    }
    asset_type_label = _asset_label.get(asset_type, asset_type)
    count_label = _asset_count_label.get(asset_type, "активов")

    estimated = estimate_duration_minutes(n_cities)
    hours = estimated // 60
    mins = estimated % 60
    duration_str = f"~{hours}ч {mins}м" if hours else f"~{mins}м"

    warning = ""
    if n_cities > 20:
        warning = (
            f"\n\n⚠️ <b>Внимание:</b> Вы создаёте {n_cities} {count_label}. "
            f"Это займёт значительное время. Убедитесь что у вас достаточно аккаунтов ({n_accs})."
        )

    # Intelligence block
    try:
        intel = await intelligence_engine.get_pre_launch_intelligence(
            pool,
            callback.from_user.id,
            "global_presence",
            n_cities,
            account_ids=selected_acc_ids if selected_acc_ids else None,
        )
        intel_text = intelligence_engine.format_pre_launch_block(intel)
        if not intel.go_decision:
            await callback.answer(intel.go_reason, show_alert=True)
            return
    except Exception:
        intel_text = ""

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=GeoPresenceCb(action="launch"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_preview"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    intel_section = f"\n\n{intel_text}" if intel_text else ""
    await _edit(
        callback,
        f"🌍 <b>Финальное подтверждение</b>\n"
        f"{'─' * 28}\n\n"
        f"<b>Что будет создано:</b>\n"
        f"📦 Тип: {asset_type_label}\n"
        f"📍 Гео: {geo_label} ({n_cities} городов)\n"
        f"🔤 Паттерн: <code>{name_pattern}</code>\n"
        f"🔗 Username: <code>{username_pattern or '—'}</code>\n"
        f"📋 Шаблон: {template_name}\n"
        f"👤 Аккаунтов: {n_accs} (round-robin)\n"
        f"⏱️ ETA: {duration_str} (safe mode)\n\n"
        f"🔢 Итого: <b>{n_cities} {count_label}</b> будет создано.\n"
        f"Операция запустится через очередь — вы получите уведомление о завершении."
        f"{warning}"
        f"{intel_section}\n\n"
        f"<b>Запустить?</b>",
        markup=kb.as_markup(),
    )


# ── Step 8: Launch ─────────────────────────────────────────────────────────


@router.callback_query(
    GeoPresenceCb.filter(F.action == "launch"), GlobalPresenceFSM.confirming
)
async def cb_gp_launch(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    # Проверка давления инфраструктуры
    ready, reason = await infra_orchestrator.is_ready_for_op(
        pool, callback.from_user.id
    )
    if not ready:
        await callback.answer(f"🚫 {reason}", show_alert=True)
        return
    warn = await infra_orchestrator.get_pressure_warning(pool, callback.from_user.id)
    await callback.answer(warn or "⏳ Создаём план…", show_alert=bool(warn))

    sd = await state.get_data()

    asset_type = sd.get("asset_type", "channel")
    name_pattern = sd.get("name_pattern", "Channel {{CITY}}")
    username_pattern = sd.get("username_pattern")
    geo_list: list[dict] = sd.get("geo_list") or []
    selected_acc_ids: list[int] = sd.get("selected_acc_ids") or []
    template_id = sd.get("template_id")
    geo_preset = sd.get("geo_preset", "")

    if not geo_list or not selected_acc_ids:
        log.warning(
            "global_presence launch rejected: missing data user_id=%s geo_count=%d selected_accounts=%d",
            callback.from_user.id,
            len(geo_list),
            len(selected_acc_ids),
        )
        await _edit(callback, "❌ Недостаточно данных для запуска. Начните сначала.")
        await state.clear()
        return

    # Determine op_type
    if asset_type == "bot":
        _op_type = "global_presence_bot"
        _effective_asset = "bot"
    elif asset_type == "package":
        _op_type = "global_presence_package"
        _effective_asset = "package"
    elif asset_type == "full_package":
        _op_type = "global_presence_full_package"
        _effective_asset = "full_package"
    else:
        _op_type = "global_presence_channel"
        _effective_asset = asset_type  # "channel" or "group"

    # Build targets (for package/full_package: channel targets first)
    targets = build_targets(
        geo_list,
        "channel" if asset_type in ("package", "full_package") else _effective_asset,
        name_pattern,
        username_pattern,
        selected_acc_ids,
    )

    # Create plan in DB
    plan_id = await db.create_global_presence_plan(
        pool,
        owner_id=callback.from_user.id,
        asset_type=_effective_asset,
        name_pattern=name_pattern,
        username_pattern=username_pattern,
        geo_selection={"preset": geo_preset, "count": len(geo_list)},
        account_selection={"account_ids": selected_acc_ids},
        template_id=template_id,
    )

    # Insert targets
    await db.create_global_presence_targets(pool, plan_id, targets)

    # Queue the primary operation
    op_id = await operation_bus.submit(
        pool,
        callback.from_user.id,
        _op_type,
        {"plan_id": plan_id},
        total_items=len(targets),
    )

    # Link operation to plan
    await db.link_plan_to_operation(pool, plan_id, op_id)
    log.info(
        "global_presence launch queued: user_id=%s asset_type=%s plan_id=%s op_id=%s targets=%d accounts=%d",
        callback.from_user.id,
        asset_type,
        plan_id,
        op_id,
        len(targets),
        len(selected_acc_ids),
    )

    # Auto-create ecosystem for this GP plan
    _eco_id: int | None = None
    try:
        from services import ecosystem_brain as _eb

        _asset_labels = {
            "channel": "Каналы",
            "group": "Группы",
            "bot": "Боты",
            "package": "Пакет",
            "full_package": "Полный пакет",
        }
        _asset_label = _asset_labels.get(asset_type, "Активы")
        _geo_label = (
            geo_preset.replace("_", " ").title()
            if geo_preset
            else f"{len(geo_list)} регионов"
        )
        _eco_name = f"GP: {_asset_label} — {_geo_label}"
        _eco_id = await _eb.create_ecosystem(
            pool,
            callback.from_user.id,
            _eco_name,
            ecosystem_type="global_presence",
            region=geo_preset or None,
        )
        await pool.execute(
            "UPDATE global_presence_plans SET ecosystem_id=$1 WHERE id=$2",
            _eco_id,
            plan_id,
        )
        await _eb.record_event(
            pool,
            _eco_id,
            callback.from_user.id,
            "plan_started",
            f"GP план #{plan_id} запущен",
            severity="info",
            details={"plan_id": plan_id, "asset_type": asset_type},
        )
        log.info(
            "global_presence: created ecosystem eco_id=%d for plan_id=%d",
            _eco_id,
            plan_id,
        )
    except Exception as _eco_err:
        log.debug("global_presence: ecosystem auto-create failed: %s", _eco_err)

    # For package/full_package: also queue group (and bot for full) creation
    op_id2, op_id3 = None, None
    if asset_type in ("package", "full_package"):
        grp_targets = build_targets(
            geo_list, "group", name_pattern, username_pattern, selected_acc_ids
        )
        plan_id2 = await db.create_global_presence_plan(
            pool,
            owner_id=callback.from_user.id,
            asset_type="group",
            name_pattern=name_pattern,
            username_pattern=username_pattern,
            geo_selection={"preset": geo_preset, "count": len(geo_list)},
            account_selection={"account_ids": selected_acc_ids},
            template_id=template_id,
        )
        await db.create_global_presence_targets(pool, plan_id2, grp_targets)
        op_id2 = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "global_presence_group",
            {"plan_id": plan_id2},
            total_items=len(grp_targets),
        )
        await db.link_plan_to_operation(pool, plan_id2, op_id2)
        log.info(
            "global_presence package group queued: user_id=%s plan_id=%s op_id=%s targets=%d",
            callback.from_user.id,
            plan_id2,
            op_id2,
            len(grp_targets),
        )

    # For full_package: also queue bot creation
    if asset_type == "full_package":
        bot_targets = build_targets(
            geo_list, "bot", name_pattern, username_pattern, selected_acc_ids
        )
        plan_id3 = await db.create_global_presence_plan(
            pool,
            owner_id=callback.from_user.id,
            asset_type="bot",
            name_pattern=name_pattern,
            username_pattern=username_pattern,
            geo_selection={"preset": geo_preset, "count": len(geo_list)},
            account_selection={"account_ids": selected_acc_ids},
            template_id=template_id,
        )
        await db.create_global_presence_targets(pool, plan_id3, bot_targets)
        op_id3 = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "global_presence_bot",
            {"plan_id": plan_id3},
            total_items=len(bot_targets),
        )
        await db.link_plan_to_operation(pool, plan_id3, op_id3)
        log.info(
            "global_presence full_package bot queued: user_id=%s plan_id=%s op_id=%s targets=%d",
            callback.from_user.id,
            plan_id3,
            op_id3,
            len(bot_targets),
        )

    await state.clear()

    # Auto-create ecosystem for this Global Presence package
    try:
        from services import ecosystem_brain as _eb

        geo_label_eco = GEO_PRESETS.get(geo_preset, {}).get(
            "label", geo_preset or "Custom"
        )
        eco_name = f"Global Presence — {geo_label_eco}"
        eco_id = await _eb.create_ecosystem(
            pool,
            owner_id=callback.from_user.id,
            name=eco_name,
            description="Автоматически создана при запуске Global Presence",
            ecosystem_type="global_presence",
            region=geo_preset if geo_preset else None,
        )
        await _eb.record_event(
            pool,
            eco_id,
            callback.from_user.id,
            "operation",
            f"Global Presence запущен: {geo_label_eco}",
            severity="info",
        )
        log.debug("ecosystem created for global_presence: %d", eco_id)
    except Exception as e:
        log.debug("ecosystem auto-create failed: %s", e)

    # Build result message
    _type_emoji = {
        "channel": "📡",
        "group": "👥",
        "bot": "🤖",
        "package": "📦",
        "full_package": "📦",
    }
    _type_label = {
        "channel": "каналов",
        "group": "групп",
        "bot": "ботов",
        "package": "пакетов",
        "full_package": "пакетов",
    }
    emoji = _type_emoji.get(asset_type, "📦")
    label = _type_label.get(asset_type, "активов")
    pkg_lines = []
    if op_id2:
        pkg_lines.append(f"👥 + Группы в очереди: #{op_id2}")
    if op_id3:
        pkg_lines.append(f"🤖 + Боты в очереди: #{op_id3}")
    pkg_line = "\n".join(pkg_lines) if pkg_lines else ""
    if pkg_line:
        pkg_line = "\n" + pkg_line

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Прогресс",
        callback_data=GeoPresenceCb(action="progress", plan_id=plan_id),
    )
    kb.button(text="📋 Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.button(text="◀️ Назад к меню", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    await _edit(
        callback,
        f"✅ <b>Global Presence Plan #{plan_id} запущен!</b>\n\n"
        f"{emoji} {label.capitalize()} для создания: {len(targets)}\n"
        f"🔢 Операция в очереди: #{op_id}{pkg_line}\n\n"
        f"Вы получите уведомление по завершении.\n"
        f"Нажмите «Прогресс» для отслеживания.",
        markup=kb.as_markup(),
    )


# ── Progress & Report ──────────────────────────────────────────────────────


@router.callback_query(GeoPresenceCb.filter(F.action == "launch"))
async def cb_gp_launch_stale(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    log.warning(
        "global_presence stale launch callback: user_id=%s state=%s data=%s",
        callback.from_user.id,
        current_state,
        callback.data,
    )
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🌍 Открыть Global Presence", callback_data=GeoPresenceCb(action="menu")
    )
    kb.adjust(1)
    await callback.answer(
        "Сессия мастера устарела. Начните запуск заново.", show_alert=True
    )
    await callback.message.edit_text(
        "⚠️ <b>Запуск не принят</b>\n\n"
        "Сессия мастера устарела или бот перезапускался между шагами. "
        "Откройте Global Presence и соберите план еще раз.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "progress"))
async def cb_gp_progress(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    plan_id = callback_data.plan_id
    if not plan_id:
        await callback.answer("Укажите ID плана", show_alert=True)
        return

    try:
        plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_gp_progress: get_global_presence_plan failed")
        await callback.answer("Ошибка загрузки плана", show_alert=True)
        return
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return
    await callback.answer()

    try:
        stats = await db.get_global_presence_stats(pool, plan_id)
        op_id = plan.get("op_id")
        op_status = "—"
        if op_id:
            op_row = await pool.fetchrow(
                "SELECT status, done_items, total_items FROM operation_queue WHERE id=$1",
                op_id,
            )
            if op_row:
                op_status = op_row["status"]
        current_row = await pool.fetchrow(
            "SELECT city FROM global_presence_targets WHERE plan_id=$1 AND status='running' LIMIT 1",
            plan_id,
        )
        current_city = current_row["city"] if current_row else "—"
    except Exception:
        log_exc_swallow(log, "cb_gp_progress: stats/operation fetch failed")
        stats = {"total": 0, "done": 0, "failed": 0, "pending": 0}
        op_id = None
        op_status = "—"
        current_city = "—"

    total = stats["total"]
    done = stats["done"]
    failed = stats["failed"]
    pending = stats["pending"]

    pct = int(done / total * 100) if total else 0
    bar_filled = pct // 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    # Estimate remaining
    remaining = pending
    estimated_remaining = estimate_duration_minutes(remaining)
    hours = estimated_remaining // 60
    mins = estimated_remaining % 60
    remaining_str = f"~{hours}ч {mins}м" if hours else f"~{mins}м"

    status_map = {
        "queued": "В очереди",
        "running": "Выполняется",
        "done": "Завершён",
        "failed": "Ошибка",
        "cancelled": "Отменён",
        "draft": "Черновик",
    }

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

    # Auto-sync plan status if operation finished but plan stuck
    synced_status = None
    if plan["status"] in ("running", "queued") and op_status in (
        "done",
        "failed",
        "cancelled",
    ):
        try:
            synced_status = await db.sync_plan_status_from_op(pool, plan_id)
            if synced_status:
                plan = await db.get_global_presence_plan(
                    pool, plan_id, callback.from_user.id
                )
        except Exception:
            log_exc_swallow(
                log,
                f"global_presence: sync_plan_status_from_op failed plan_id={plan_id}",
            )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Обновить",
        callback_data=GeoPresenceCb(action="progress", plan_id=plan_id),
    )
    if plan["status"] in ("running", "queued"):
        kb.button(
            text="🚫 Отменить план",
            callback_data=GeoPresenceCb(action="cancel_plan", plan_id=plan_id),
        )
    if failed > 0:
        kb.button(
            text="🔁 Повторить ошибки",
            callback_data=GeoPresenceCb(action="retry", plan_id=plan_id),
        )
    kb.button(
        text="📋 Отчёт", callback_data=GeoPresenceCb(action="report", plan_id=plan_id)
    )
    kb.button(text="◀️ Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.adjust(2)

    if synced_status:
        sync_note = {
            "done": "✅ завершён",
            "failed": "❌ ошибка",
            "cancelled": "🚫 отменён",
        }.get(synced_status, synced_status)
        text += f"\n\n<i>Статус синхронизирован: операция {sync_note}</i>"

    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(GeoPresenceCb.filter(F.action == "retry"))
async def cb_gp_retry(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    plan_id = callback_data.plan_id
    try:
        plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_gp_retry: get_global_presence_plan failed")
        await callback.answer("Ошибка загрузки плана", show_alert=True)
        return
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return

    try:
        reset_count = await db.reset_failed_targets(pool, plan_id)
    except Exception:
        log_exc_swallow(log, "cb_gp_retry: reset_failed_targets failed")
        await callback.answer("Ошибка сброса целей", show_alert=True)
        return
    if reset_count == 0:
        await callback.answer("Нет повторяемых ошибок", show_alert=True)
        return

    # Determine correct op_type from plan's asset_type
    _asset = plan["asset_type"] if plan else "channel"
    if _asset == "bot":
        _retry_op_type = "global_presence_bot"
    elif _asset == "package":
        _retry_op_type = "global_presence_package"
    elif _asset == "full_package":
        _retry_op_type = "global_presence_full_package"
    else:
        _retry_op_type = "global_presence_channel"

    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            _retry_op_type,
            {"plan_id": plan_id},
            total_items=reset_count,
        )
        await db.link_plan_to_operation(pool, plan_id, op_id)
        log.info(
            "cb_gp_retry: plan=%d reset=%d op=%d user=%s",
            plan_id,
            reset_count,
            op_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "cb_gp_retry: operation_queue insert failed")
        await callback.answer("Ошибка постановки в очереди", show_alert=True)
        return
    await callback.answer(
        f"✅ {reset_count} целей поставлено в очередь на повтор (op #{op_id})",
        show_alert=True,
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "report"))
async def cb_gp_report(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    plan_id = callback_data.plan_id
    try:
        plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_gp_report: get_global_presence_plan failed")
        await callback.answer("Ошибка загрузки плана", show_alert=True)
        return
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return
    await callback.answer()

    try:
        stats = await db.get_global_presence_stats(pool, plan_id)
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
    except Exception:
        log_exc_swallow(log, "cb_gp_report: stats/targets fetch failed")
        stats = {"total": 0, "done": 0, "failed": 0, "pending": 0}
        done_targets = []
        failed_targets = []

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
    kb.button(
        text="◀️ Прогресс",
        callback_data=GeoPresenceCb(action="progress", plan_id=plan_id),
    )
    kb.adjust(1)
    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(GeoPresenceCb.filter(F.action == "cancel_plan"))
async def cb_gp_cancel_plan(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    """Cancel a running or queued global presence plan."""
    plan_id = callback_data.plan_id
    if not plan_id:
        await callback.answer("Укажите ID плана", show_alert=True)
        return

    try:
        plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_gp_cancel_plan: get_global_presence_plan failed")
        await callback.answer("Ошибка загрузки плана", show_alert=True)
        return
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return

    if plan["status"] in ("done", "cancelled", "failed"):
        await callback.answer(f"План уже завершён ({plan['status']})", show_alert=True)
        return

    try:
        ok = await db.cancel_global_presence_plan(pool, plan_id, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_gp_cancel_plan: cancel failed")
        await callback.answer("Ошибка при отмене", show_alert=True)
        return

    if not ok:
        await callback.answer("Не удалось отменить план", show_alert=True)
        return

    await callback.answer("🚫 План отменён")
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.button(
        text="📊 Прогресс",
        callback_data=GeoPresenceCb(action="progress", plan_id=plan_id),
    )
    kb.adjust(2)
    await _edit(
        callback,
        f"🚫 <b>Global Presence Plan #{plan_id} отменён</b>\n\n"
        f"Незавершённые операции остановлены.\n"
        f"Уже созданные каналы/группы остаются активными.",
        markup=kb.as_markup(),
    )


# ── Plans List ─────────────────────────────────────────────────────────────


@router.callback_query(GeoPresenceCb.filter(F.action == "plans_list"))
async def cb_gp_plans_list(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    try:
        plans = await db.get_global_presence_plans(pool, callback.from_user.id, limit=8)
    except Exception:
        log_exc_swallow(log, "cb_gp_plans_list: get_global_presence_plans failed")
        plans = []
    if not plans:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Создать план", callback_data=GeoPresenceCb(action="menu"))
        kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="cancel"))
        kb.adjust(1)
        await _edit(
            callback,
            "🌍 <b>Global Presence Factory</b>\n\n"
            "У вас ещё нет планов присутствия.\n"
            "Нажмите «Создать план» чтобы начать.",
            markup=kb.as_markup(),
        )
        return

    import re as _re
    import json as _json

    status_emoji = {
        "queued": "⏳",
        "running": "⚡",
        "done": "✅",
        "failed": "❌",
        "cancelled": "🚫",
        "draft": "📝",
    }
    kb = InlineKeyboardBuilder()
    for plan in plans:
        emoji = status_emoji.get(plan["status"], "❓")
        geo_sel = (
            plan["geo_selection"]
            if isinstance(plan["geo_selection"], dict)
            else _json.loads(plan["geo_selection"] or "{}")
        )
        count = geo_sel.get("count", "?")
        # Strip {{PLACEHOLDER}} syntax from name_pattern for cleaner display
        display_name = _re.sub(
            r"\{\{[^}]+\}\}", "[город]", plan["name_pattern"] or ""
        ).strip()[:24]
        label = f"{emoji} #{plan['id']} — {display_name} ({count} городов)"
        kb.button(
            text=label,
            callback_data=GeoPresenceCb(action="progress", plan_id=plan["id"]),
        )
    kb.button(text="➕ Новый план", callback_data=GeoPresenceCb(action="menu"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="cancel"))
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
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_uname"))
async def cb_gp_back_uname(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.entering_username_pattern)
    sd = await state.get_data()
    await _show_username_pattern_step(
        callback, state, prefill=sd.get("username_pattern")
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_tpl"))
async def cb_gp_back_tpl(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.choosing_template)
    await _show_template_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_acc"))
async def cb_gp_back_acc(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    await state.set_state(GlobalPresenceFSM.choosing_accounts)
    await _show_accounts_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_preview"))
async def cb_gp_back_preview(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
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
    await _edit(
        callback, "❌ <b>Global Presence Factory</b> — отменено.", markup=kb.as_markup()
    )

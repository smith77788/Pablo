"""Global Presence Factory — guided FSM wizard for worldwide Telegram channel creation."""

from __future__ import annotations

import asyncio
import html
import logging
import random

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, GeoPresenceCb
from bot.states import GlobalPresenceFSM
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
from database import db
from services.geo_data import GEO_PRESETS, parse_custom_geo_list, enrich_geo_list
from services.presence_planner import (
    render_pattern,
    estimate_duration_minutes,
)
from services.username_engine import slugify
from services.logger import log_exc_swallow
from services import (
    operation_bus,
    infra_orchestrator,
    intelligence_engine,
    infra_generator,
    avatar_factory,
)
from bot.utils.op_helpers import safe_answer

log = logging.getLogger(__name__)
router = Router()

_TPL_PAGE_SIZE = 5
_ACC_PAGE_SIZE = 8


# ── Helpers ────────────────────────────────────────────────────────────────


async def _edit(cb: CallbackQuery, text: str, markup=None) -> None:
    try:
        await cb.answer()
    except Exception:
        log.debug(
            "global_presence: callback answer already sent or expired", exc_info=True
        )
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return
        if "there is no text in the message to edit" in err_str:
            try:
                await cb.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
                return
            except Exception as e:
                log.warning("handler error in _edit: %s", e)
        if "message to edit not found" in err_str or "message can't be edited" in err_str:
            await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup, parse_mode="HTML")
        else:
            log.warning("global_presence _edit error: %s", e)


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
        await state.clear()
        await safe_answer(callback)
        await callback.message.edit_text(
            locked_text("Global Presence Factory", "enterprise"),
            reply_markup=subscription_locked_markup("enterprise", back_callback=BmCb(action="visibility")),
        )
        return
    await safe_answer(callback)
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
        plans_hint = f"\n⚡ <b>Активных операций: {running_count}</b> — откройте «📋 Мои планы» ниже\n"
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
        # Показать причину ошибки, чтобы было понятно что делать
        if last["status"] == "failed":
            reason = None
            try:
                reason = await pool.fetchval(
                    "SELECT error_message FROM global_presence_targets "
                    "WHERE plan_id=$1 AND error_message IS NOT NULL AND error_message<>'' "
                    "ORDER BY id DESC LIMIT 1",
                    last["id"],
                )
            except Exception:
                log_exc_swallow(log, "cb_gp_menu: fetch target error failed")
            if reason:
                plans_hint += f"<i>Причина: {reason[:120]}</i>\n"
            plans_hint += "<i>Частые причины: нет активных аккаунтов с сессией, FloodWait, аккаунт в кулдауне. Откройте «Мои планы» для деталей.</i>\n"

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
    kb.button(
        text="🏗 Своя структура города",
        callback_data=GeoPresenceCb(action="asset", item="custom"),
    )
    if recent_plans:
        kb.button(text="📋 Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(2, 1, 1, 1, 1, 1)
    await callback.message.edit_text(
        f"🌍 <b>Гео-сеть: создать</b> (Global Presence)\n"
        f"{'─' * 28}\n"
        f"Бот <b>создаёт НОВУЮ</b> Telegram-инфраструктуру сразу в сотнях городов — "
        f"каналы, группы и боты с локализованными названиями, username, "
        f"описаниями и аватарами.\n"
        f"<i>Если нужно объединить уже существующие активы — это «🔗 Связки».</i>\n"
        f"{plans_hint}\n"
        f"<b>Шаг 1 — Что создаём в каждом городе:</b>\n"
        f"📡 <b>Каналы</b> — публичные каналы под каждый город\n"
        f"👥 <b>Группы</b> — супергруппы для обсуждений\n"
        f"🤖 <b>Боты</b> — боты через BotFather (нужны аккаунты)\n"
        f"📦 <b>Пакет</b> — канал <i>и</i> группа на каждый город\n"
        f"🌐 <b>Полный пакет</b> — канал + группа + бот на каждый город\n"
        f"🏗 <b>Своя структура</b> — выбрать тематики: новости, чат, работа, "
        f"афиша, барахолка, недвижимость, авто, услуги…",
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
    if asset == "custom":
        await safe_answer(callback)
        await state.update_data(asset_type="custom", roles=list(_DEFAULT_ROLES))
        await state.set_state(GlobalPresenceFSM.choosing_structure)
        await _show_structure_step(callback, state)
        return
    if asset not in ("channel", "group", "bot", "package", "full_package"):
        await callback.answer("Неподдерживаемый тип", show_alert=True)
        return
    await safe_answer(callback)
    # Готовый тип — это тоже структура, просто предопределённая: так дальше по
    # флоу работает ОДИН генератор, без второй ветки «старый путь».
    await state.update_data(asset_type=asset, roles=list(ASSET_ROLE_PRESETS[asset]))
    await state.set_state(GlobalPresenceFSM.choosing_template)
    await _show_template_step(callback, state, pool, asset_type=asset, page=0)


# ── Структура города (роли) ─────────────────────────────────────────────────

# Готовые типы разложены в роли: «Пакет» — это новости + чат, «Полный пакет» —
# ещё и бот. Дальше по флоу различий между «готовым типом» и «своей структурой»
# нет, поэтому генератор и предпросмотр одни на всех.
ASSET_ROLE_PRESETS: dict[str, tuple[str, ...]] = {
    "channel": ("news",),
    "group": ("chat",),
    "bot": ("bot",),
    "package": ("news", "chat"),
    "full_package": ("news", "chat", "bot"),
}

_DEFAULT_ROLES: tuple[str, ...] = ("news", "chat")


def _selected_roles(sd: dict) -> list[str]:
    roles = [r for r in (sd.get("roles") or []) if r in infra_generator.ROLE_LIBRARY]
    return roles or list(_DEFAULT_ROLES)


async def _show_structure_step(callback: CallbackQuery, state: FSMContext) -> None:
    sd = await state.get_data()
    selected = _selected_roles(sd)

    kb = InlineKeyboardBuilder()
    for key, spec in infra_generator.ROLE_LIBRARY.items():
        mark = "✅" if key in selected else "⬜"
        type_hint = {"channel": "канал", "group": "группа", "bot": "бот"}.get(
            spec["asset_type"], spec["asset_type"]
        )
        kb.button(
            text=f"{mark} {spec['label']} ({type_hint})",
            callback_data=GeoPresenceCb(action="role_tog", item=key),
        )
    kb.adjust(2)

    presets = InlineKeyboardBuilder()
    for key, preset in infra_generator.STRUCTURE_PRESETS.items():
        presets.button(
            text=preset["label"], callback_data=GeoPresenceCb(action="role_preset", item=key)
        )
    presets.adjust(2)
    kb.attach(presets)

    nav = InlineKeyboardBuilder()
    nav.button(text="➡️ Далее", callback_data=GeoPresenceCb(action="roles_done"))
    nav.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="menu"))
    nav.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    nav.adjust(1, 2)
    kb.attach(nav)

    lines = []
    for role in selected:
        spec = infra_generator.ROLE_LIBRARY[role]
        example = spec["name_patterns"][0].replace("{{CITY_NAME}}", "Самара")
        example = example.replace("{{CITY_LOC}}", "Самаре").replace("{{CITY_GEN}}", "Самары")
        lines.append(f"  • {spec['label']} → <i>{html.escape(example)}</i>")
    structure_text = "\n".join(lines) if lines else "  <i>ничего не выбрано</i>"

    await _edit(
        callback,
        f"🏗 <b>Структура города</b>\n"
        f"{'─' * 28}\n"
        f"Отметьте, какие объекты создать <b>в каждом городе</b>. "
        f"У каждой тематики свои названия, username и описания — "
        f"их не нужно придумывать.\n\n"
        f"<b>Выбрано ({len(selected)} на город):</b>\n{structure_text}\n\n"
        f"<i>Пример показан для Самары.</i>",
        markup=kb.as_markup(),
    )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "role_tog"), GlobalPresenceFSM.choosing_structure
)
async def cb_gp_role_toggle(
    callback: CallbackQuery, callback_data: GeoPresenceCb, state: FSMContext
) -> None:
    await safe_answer(callback)
    role = callback_data.item or ""
    if role not in infra_generator.ROLE_LIBRARY:
        return
    sd = await state.get_data()
    roles = list(sd.get("roles") or [])
    if role in roles:
        roles.remove(role)
    else:
        roles.append(role)
    await state.update_data(roles=roles)
    await _show_structure_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "role_preset"), GlobalPresenceFSM.choosing_structure
)
async def cb_gp_role_preset(
    callback: CallbackQuery, callback_data: GeoPresenceCb, state: FSMContext
) -> None:
    await safe_answer(callback)
    preset = infra_generator.STRUCTURE_PRESETS.get(callback_data.item or "")
    if not preset:
        return
    await state.update_data(roles=list(preset["roles"]))
    await _show_structure_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "roles_done"), GlobalPresenceFSM.choosing_structure
)
async def cb_gp_roles_done(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    sd = await state.get_data()
    if not [r for r in (sd.get("roles") or []) if r in infra_generator.ROLE_LIBRARY]:
        await callback.answer("⚠️ Выберите хотя бы одну тематику", show_alert=True)
        return
    await safe_answer(callback)
    # Структуру можно править и с экрана предпросмотра — тогда туда и
    # возвращаемся, а не отправляем пользователя заново по всему мастеру.
    if sd.get("geo_list") and sd.get("selected_acc_ids"):
        await state.set_state(GlobalPresenceFSM.previewing)
        await _show_preview(callback, state, pool)
        return
    await state.set_state(GlobalPresenceFSM.choosing_template)
    await _show_template_step(callback, state, pool, asset_type="channel", page=0)


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
    try:
        templates = await pool.fetch(
            "SELECT id, name FROM asset_templates WHERE owner_id=$1 AND asset_type=$2 "
            "ORDER BY created_at DESC LIMIT $3 OFFSET $4",
            user_id,
            asset_type,
            _TPL_PAGE_SIZE + 1,
            offset,
        )
    except Exception:
        templates = []
    has_more = len(templates) > _TPL_PAGE_SIZE
    templates = templates[:_TPL_PAGE_SIZE]

    kb = InlineKeyboardBuilder()

    # Library presets on first page (top-3)
    lib_count = 0
    if page == 0:
        lib_atype = (
            asset_type if asset_type not in ("package", "full_package") else "channel"
        )
        lib_presets = get_presets(lib_atype)[:10]
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
    await safe_answer(callback)
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
        await safe_answer(callback)
        import json as _json

        await state.update_data(
            template_id=None,
            template_name=preset["name"],
            template_data=_json.dumps(preset["template"]),
        )
    else:
        tpl_id = int(item) if item.isdigit() else 0
        try:
            tpl = await pool.fetchrow(
                "SELECT id, name, template FROM asset_templates WHERE id=$1 AND owner_id=$2",
                tpl_id,
                callback.from_user.id,
            )
        except Exception:
            tpl = None
        if not tpl:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        await safe_answer(callback)
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
    await safe_answer(callback)
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
        "Новости {{CITY_NAME}}",
        "Работа в {{CITY_LOC}}",
        "{{CITY_NAME}} • Афиша",
        "Crypto {{CITY}} {{COUNTRY_CODE}}",
    ]
    ex_text = "\n".join(f"  • <code>{e}</code>" for e in examples)
    bot_note = (
        "\n\n💡 <i>Для ботов: название — отображаемое имя в Telegram (не username).</i>"
        if asset_type in ("bot", "full_package")
        else ""
    )
    roles = _selected_roles(sd)
    lib_preview = []
    for role in roles[:3]:
        spec = infra_generator.ROLE_LIBRARY[role]
        n_names = len(spec["name_patterns"])
        lib_preview.append(f"  • {spec['label']}: {n_names} вариантов названий")
    lib_text = "\n".join(lib_preview)

    kb = InlineKeyboardBuilder()
    # Библиотека — рекомендуемый путь: пул из 5–10 названий на тематику даёт
    # разнообразие, которого один введённый паттерн дать не может физически.
    kb.button(
        text="📚 Взять из библиотеки (рекомендуется)",
        callback_data=GeoPresenceCb(action="use_library"),
    )
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_tpl"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1, 2)
    await _edit(
        callback,
        f"🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг — Названия</b>\n\n"
        f"📚 <b>Библиотека тематик</b> подберёт названия сама, разные для "
        f"каждого города:\n{lib_text}\n"
        f"<i>Одинаковые названия на сотнях объектов — первое, по чему сеть "
        f"опознаётся. Библиотека этого избегает.</i>\n\n"
        f"Либо введите <b>свой шаблон</b> для названия {asset_noun}:\n"
        f"  <code>{{{{CITY_NAME}}}}</code> — город на родном языке (Москва, Київ)\n"
        f"  <code>{{{{CITY_GEN}}}}</code> — родительный падеж (Москвы, Самары)\n"
        f"  <code>{{{{CITY_LOC}}}}</code> — предложный падеж (Москве, Самаре)\n"
        f"  <code>{{{{CITY}}}}</code> — город латиницей\n"
        f"  <code>{{{{COUNTRY}}}}</code> / <code>{{{{COUNTRY_CODE}}}}</code> — страна / код\n"
        f"  <code>{{{{CITY_SLUG}}}}</code> — транслит-слаг (для username)\n"
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
    await safe_answer(callback)
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
    await safe_answer(callback)
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
    await safe_answer(callback)
    sd = await state.get_data()
    await state.update_data(
        username_pattern=sd.get("username_pattern_pending", ""),
        username_pattern_pending=None,
        no_username=False,
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
    await safe_answer(callback)
    sd = await state.get_data()
    await _show_username_pattern_step(
        callback, state, prefill=sd.get("username_pattern_pending")
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "skip_uname"))
async def cb_gp_skip_uname(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    # no_username — ОСОЗНАННЫЙ отказ. Пустой username_pattern сам по себе
    # означает «возьми шаблоны из библиотеки», поэтому нужен отдельный флаг:
    # иначе «Без username» молча выдавал бы имена из библиотеки тематики.
    await state.update_data(
        username_pattern=None, username_pattern_pending=None, no_username=True
    )
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "use_library"),
    GlobalPresenceFSM.entering_name_pattern,
)
async def cb_gp_use_library(callback: CallbackQuery, state: FSMContext) -> None:
    """Пулы названий/username/описаний берутся из библиотеки выбранных тематик.

    Пустые пулы = сигнал генератору «используй библиотечные», поэтому здесь
    достаточно их обнулить и пропустить оба шага ручного ввода.
    """
    await safe_answer(callback)
    await state.update_data(
        name_pattern=None,
        username_pattern=None,
        name_pattern_pending=None,
        username_pattern_pending=None,
        no_username=False,
        use_library=True,
    )
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


# ── Уровни географии ───────────────────────────────────────────────────────


def _selected_levels(sd: dict) -> list[str]:
    levels = [lv for lv in (sd.get("levels") or []) if lv in infra_generator.LEVELS]
    return levels or [infra_generator.LEVEL_CITY]


async def _show_levels_step(callback: CallbackQuery, state: FSMContext) -> None:
    sd = await state.get_data()
    selected = _selected_levels(sd)
    geo_list = sd.get("geo_list") or []
    roles = _selected_roles(sd)

    # Показываем реальное число объектов на каждом уровне — «сколько это будет»
    # должно быть видно ДО запуска, а не выясняться в процессе.
    counts = {}
    for lv in infra_generator.LEVELS:
        nodes = infra_generator.expand_geo_levels(geo_list, [lv])
        counts[lv] = len(nodes) * len(roles)

    kb = InlineKeyboardBuilder()
    for key, spec in infra_generator.LEVELS.items():
        mark = "✅" if key in selected else "⬜"
        kb.button(
            text=f"{mark} {spec['label']} — {counts.get(key, 0)} шт.",
            callback_data=GeoPresenceCb(action="lvl_tog", item=key),
        )
    kb.button(text="➡️ Далее", callback_data=GeoPresenceCb(action="lvl_done"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_geo"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1, 1, 1, 1, 2)

    total = sum(counts.get(lv, 0) for lv in selected)
    await _edit(
        callback,
        f"🗺 <b>Уровни присутствия</b>\n"
        f"{'─' * 28}\n"
        f"Объекты можно создать не только по городам, но и «зонтиками» "
        f"над ними:\n\n"
        f"🏛 <b>Федеральный</b> — один объект на страну\n"
        f"🗺 <b>Региональный</b> — один на область/край\n"
        f"🏙 <b>Городской</b> — на каждый город\n\n"
        f"<i>Регионы берутся из гео-данных; у городов без указанного региона "
        f"региональный уровень пропускается.</i>\n\n"
        f"<b>Итого к созданию: {total}</b>",
        markup=kb.as_markup(),
    )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "lvl_tog"), GlobalPresenceFSM.choosing_levels
)
async def cb_gp_level_toggle(
    callback: CallbackQuery, callback_data: GeoPresenceCb, state: FSMContext
) -> None:
    await safe_answer(callback)
    lv = callback_data.item or ""
    if lv not in infra_generator.LEVELS:
        return
    sd = await state.get_data()
    levels = list(sd.get("levels") or [infra_generator.LEVEL_CITY])
    if lv in levels:
        levels.remove(lv)
    else:
        levels.append(lv)
    await state.update_data(levels=levels)
    await _show_levels_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "lvl_done"), GlobalPresenceFSM.choosing_levels
)
async def cb_gp_levels_done(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    sd = await state.get_data()
    if not [lv for lv in (sd.get("levels") or []) if lv in infra_generator.LEVELS]:
        await callback.answer("⚠️ Выберите хотя бы один уровень", show_alert=True)
        return
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.previewing)
    await _show_preview(callback, state, pool)


# ── Стиль аватаров ─────────────────────────────────────────────────────────


async def _show_avatar_step(callback: CallbackQuery, state: FSMContext) -> None:
    sd = await state.get_data()
    current = sd.get("avatar_style") or avatar_factory.DEFAULT_STYLE

    kb = InlineKeyboardBuilder()
    for key, spec in avatar_factory.AVATAR_STYLES.items():
        mark = "✅" if key == current else "⬜"
        kb.button(
            text=f"{mark} {spec['label']}",
            callback_data=GeoPresenceCb(action="av_style", item=key),
        )
    kb.adjust(2)
    nav = InlineKeyboardBuilder()
    nav.button(text="👁 Показать примеры", callback_data=GeoPresenceCb(action="av_preview"))
    nav.button(text="🚫 Без аватаров", callback_data=GeoPresenceCb(action="av_style", item="none"))
    nav.button(text="◀️ К предпросмотру", callback_data=GeoPresenceCb(action="back_to_preview"))
    nav.adjust(1)
    kb.attach(nav)

    if current == "none":
        note = (
            "⚠️ <b>Без аватаров.</b> Канал без фото Telegram считает заготовкой: "
            "хуже ранжируется и чаще ловит ограничения."
        )
    else:
        space = avatar_factory.variant_space(current)
        note = f"Комбинаций в этом стиле: <b>{space:,}</b>".replace(",", " ")

    font_warn = (
        ""
        if avatar_factory.fonts_available()
        else "\n\n⚠️ <i>В системе нет шрифтов — инициалы на аватаре будут нечитаемы. "
        "Сообщите администратору.</i>"
    )

    await _edit(
        callback,
        f"🖼 <b>Аватары</b>\n"
        f"{'─' * 28}\n"
        f"Каждый объект получит <b>свою</b> картинку в едином стиле проекта. "
        f"Одинаковый аватар на сотнях каналов ищется по хешу изображения и "
        f"выдаёт всю сеть разом.\n\n"
        f"{note}{font_warn}",
        markup=kb.as_markup(),
    )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "av_style"), GlobalPresenceFSM.choosing_avatar
)
async def cb_gp_avatar_style(
    callback: CallbackQuery, callback_data: GeoPresenceCb, state: FSMContext
) -> None:
    await safe_answer(callback)
    style = callback_data.item or avatar_factory.DEFAULT_STYLE
    if style != "none" and style not in avatar_factory.AVATAR_STYLES:
        style = avatar_factory.DEFAULT_STYLE
    await state.update_data(avatar_style=style)
    await _show_avatar_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "av_preview"), GlobalPresenceFSM.choosing_avatar
)
async def cb_gp_avatar_preview(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Контактный лист аватаров: пользователь видит разнообразие до запуска."""
    await safe_answer(callback)
    sd = await state.get_data()
    style = sd.get("avatar_style") or avatar_factory.DEFAULT_STYLE
    if style == "none":
        await callback.answer("Аватары отключены", show_alert=True)
        return

    try:
        targets = await _generate_targets(sd, pool, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "cb_gp_avatar_preview: сборка целей не удалась")
        targets = []
    if not targets:
        await callback.answer(
            "Сначала выберите географию — примеры строятся по реальным городам",
            show_alert=True,
        )
        return

    sample = targets[:8]
    try:
        sheet = await asyncio.to_thread(
            avatar_factory.generate_preview_sheet,
            [t["avatar_seed"] for t in sample],
            [t["planned_name"] for t in sample],
            style=style,
            cell=180,
        )
    except Exception as exc:
        log.warning("global_presence: не удалось построить лист аватаров: %s", exc)
        await callback.answer("Не удалось построить примеры", show_alert=True)
        return

    from aiogram.types import BufferedInputFile

    caption_names = "\n".join(f"  • {html.escape(t['planned_name'])}" for t in sample[:8])
    try:
        await callback.message.answer_photo(
            BufferedInputFile(sheet, filename="avatars.png"),
            caption=f"🖼 <b>Примеры аватаров</b> (стиль: "
            f"{avatar_factory.AVATAR_STYLES[style]['label']})\n\n{caption_names}\n\n"
            f"<i>Именно эти картинки и встанут на объекты — генерация "
            f"детерминирована.</i>",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("global_presence: отправка листа аватаров не удалась: %s", exc)
        await callback.answer("Не удалось отправить примеры", show_alert=True)


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
    await safe_answer(callback)
    await state.update_data(geo_preset=preset_key, geo_list=preset["cities"])
    # Если для городов пресета есть данные о населении — предложить фильтр
    # «города > N» (сценарий «все города с населением >50 000»). Иначе сразу далее.
    from services.geo_data import city_population
    if any(city_population(c.get("city_slug", "")) for c in preset["cities"]):
        await _show_pop_filter_step(callback, state)
        return
    await state.set_state(GlobalPresenceFSM.choosing_accounts)
    await _show_accounts_step(callback, state, pool, page=0)


async def _show_pop_filter_step(callback: CallbackQuery, state: FSMContext) -> None:
    """Фильтр по населению для пресета с данными о населении."""
    from services.geo_data import filter_by_population
    sd = await state.get_data()
    preset = GEO_PRESETS.get(sd.get("geo_preset", ""), {})
    cities = preset.get("cities", [])
    thresholds = [
        ("Все города", 0),
        ("> 1 млн", 1_000_000),
        ("> 500 тыс", 500_000),
        ("> 100 тыс", 100_000),
        ("> 50 тыс", 50_000),
    ]
    kb = InlineKeyboardBuilder()
    for label, thr in thresholds:
        n = len(filter_by_population(cities, thr)) if thr else len(cities)
        if thr and n == 0:
            continue  # не показываем пустые пороги
        kb.button(
            text=f"{label} ({n})",
            callback_data=GeoPresenceCb(action="pop", item=str(thr)),
        )
    # Выбор по федеральным округам (дерево ФО→регион→город) — если данные есть.
    from services.geo_data import federal_district
    if any(federal_district(c.get("region", "")) for c in cities):
        kb.button(text="🗂 По федеральным округам", callback_data=GeoPresenceCb(action="districts"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_geo"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)
    await _edit(
        callback,
        "🌍 <b>Global Presence Factory</b>\n\n"
        f"<b>Шаг 5/8 — Уточнение географии</b>\n"
        f"Пресет: <b>{html.escape(preset.get('label', ''))}</b>\n\n"
        "Отфильтровать по размеру города или выбрать федеральный округ? "
        "Города без данных о населении остаются только в «Все города».",
        markup=kb.as_markup(),
    )


def _sorted_districts(cities: list[dict]) -> list[str]:
    """Стабильный порядок федеральных округов пресета (для индексации в callback)."""
    from services.geo_data import group_by_federal_district
    return sorted(group_by_federal_district(cities).keys())


async def _show_district_step(callback: CallbackQuery, state: FSMContext) -> None:
    from services.geo_data import group_by_federal_district
    sd = await state.get_data()
    preset = GEO_PRESETS.get(sd.get("geo_preset", ""), {})
    cities = preset.get("cities", [])
    tree = group_by_federal_district(cities)
    districts = sorted(tree.keys())
    kb = InlineKeyboardBuilder()
    for i, d in enumerate(districts):
        n = sum(len(v) for v in tree[d].values())
        kb.button(text=f"{d} ({n})", callback_data=GeoPresenceCb(action="district", item=str(i)))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_pop"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)
    await _edit(
        callback,
        "🌍 <b>Global Presence Factory</b>\n\n"
        "<b>Шаг 5/8 — Федеральный округ</b>\n\n"
        "Выберите округ — в проект войдут все его города:",
        markup=kb.as_markup(),
    )


@router.callback_query(
    GeoPresenceCb.filter(F.action == "districts"), GlobalPresenceFSM.choosing_geo
)
async def cb_gp_districts(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await _show_district_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "district"), GlobalPresenceFSM.choosing_geo
)
async def cb_gp_district_pick(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
    from services.geo_data import federal_district
    sd = await state.get_data()
    preset = GEO_PRESETS.get(sd.get("geo_preset", ""), {})
    cities = preset.get("cities", [])
    districts = _sorted_districts(cities)
    try:
        picked = districts[int(callback_data.item or -1)]
    except (TypeError, ValueError, IndexError):
        await callback.answer("Округ не найден", show_alert=True)
        return
    filtered = [
        c for c in cities
        if (federal_district(c.get("region", "")) or c.get("region")) == picked
    ]
    if not filtered:
        await callback.answer("В округе нет городов", show_alert=True)
        return
    await state.update_data(geo_list=filtered)
    await state.set_state(GlobalPresenceFSM.choosing_accounts)
    await _show_accounts_step(callback, state, pool, page=0)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "back_to_pop"), GlobalPresenceFSM.choosing_geo
)
async def cb_gp_back_to_pop(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await _show_pop_filter_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "pop"), GlobalPresenceFSM.choosing_geo
)
async def cb_gp_pop_filter(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
    try:
        thr = int(callback_data.item or 0)
    except (TypeError, ValueError):
        thr = 0
    sd = await state.get_data()
    preset = GEO_PRESETS.get(sd.get("geo_preset", ""), {})
    cities = preset.get("cities", [])
    if thr > 0:
        from services.geo_data import filter_by_population
        cities = filter_by_population(cities, thr)
    if not cities:
        await callback.answer("Нет городов под этот порог", show_alert=True)
        return
    await state.update_data(geo_list=cities)
    await state.set_state(GlobalPresenceFSM.choosing_accounts)
    await _show_accounts_step(callback, state, pool, page=0)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "back_to_geo"), GlobalPresenceFSM.choosing_geo
)
async def cb_gp_back_to_geo(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await _show_geo_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "geo_custom"), GlobalPresenceFSM.choosing_geo
)
async def cb_gp_geo_custom(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
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
            f"в разделе <b>/menu → 🏗 Активы & Сети → 📱 TG-аккаунты</b>.\n\n"
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
            except Exception as _ea:
                _eas = str(_ea).lower()
                if "message to edit not found" in _eas or "message can't be edited" in _eas:
                    if hasattr(callback, "bot") and callback.bot is not None:
                        await callback.bot.send_message(
                            callback.from_user.id, no_acc_text,
                            reply_markup=no_acc_kb.as_markup(), parse_mode="HTML"
                        )
                    else:
                        log.warning("global_presence: no bot on callback, cannot send fallback")
                elif "message is not modified" not in _eas:
                    log.warning("global_presence acc fallback edit error: %s", _ea)
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
        except Exception as _e2:
            _e2s = str(_e2).lower()
            if ("message to edit not found" in _e2s or "message can't be edited" in _e2s) and hasattr(callback, "bot"):
                await callback.bot.send_message(
                    callback.from_user.id, text, reply_markup=kb.as_markup(), parse_mode="HTML"
                )
            else:
                log.warning("global_presence inline edit error: %s", _e2)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "acc_page"), GlobalPresenceFSM.choosing_accounts
)
async def cb_gp_acc_page(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
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
    await safe_answer(callback)
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
    await safe_answer(callback)
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
    await safe_answer(callback)
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
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.previewing)
    await _show_preview(callback, state, pool)


# ── Step 7: Preview ────────────────────────────────────────────────────────


def _ensure_plan_seed(sd: dict) -> int:
    """Зерно плана. Оно определяет ВСЁ содержимое (имена, описания, аватары),
    поэтому фиксируется один раз и переживает возвраты по мастеру — иначе
    каждое открытие предпросмотра показывало бы новый набор объектов."""
    seed = sd.get("plan_seed")
    if not seed:
        seed = random.randrange(1, 2**31)
    return int(seed)


async def _generate_targets(sd: dict, pool: asyncpg.Pool, owner_id: int) -> list[dict]:
    """Собрать цели плана генератором. Пустой список — гео ещё не выбрано.

    Занятые username подтягиваются из БД, чтобы аллокатор не выдал имя, уже
    стоящее на канале владельца: такую коллизию видно заранее, и падать из-за
    неё в бою незачем.
    """
    geo_list = enrich_geo_list(sd.get("geo_list") or [])
    if not geo_list:
        return []

    name_pattern = (sd.get("name_pattern") or "").strip()
    username_pattern = (sd.get("username_pattern") or "").strip()
    avatar_style = sd.get("avatar_style") or avatar_factory.DEFAULT_STYLE

    taken = await db.get_taken_usernames(pool, owner_id)

    return infra_generator.build_project_targets(
        geo_list,
        roles=_selected_roles(sd),
        levels=_selected_levels(sd),
        name_pool=[name_pattern] if name_pattern else None,
        username_pool=[username_pattern] if username_pattern else None,
        account_ids=sd.get("selected_acc_ids") or [],
        plan_seed=_ensure_plan_seed(sd),
        taken_usernames=taken,
        avatar_style=None if avatar_style == "none" else avatar_style,
        assign_usernames=not sd.get("no_username"),
    )


def _structure_line(sd: dict) -> str:
    roles = _selected_roles(sd)
    return ", ".join(
        infra_generator.ROLE_LIBRARY[r]["label"] for r in roles
    ) or "—"


def _levels_line(sd: dict) -> str:
    return ", ".join(
        infra_generator.LEVELS[lv]["label"] for lv in _selected_levels(sd)
    )


async def _show_preview(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    sd = await state.get_data()
    seed = _ensure_plan_seed(sd)
    if sd.get("plan_seed") != seed:
        await state.update_data(plan_seed=seed)
        sd["plan_seed"] = seed

    geo_preset = sd.get("geo_preset", "")
    selected_acc_ids: list[int] = sd.get("selected_acc_ids") or []
    template_name = sd.get("template_name") or "Нет"
    avatar_style = sd.get("avatar_style") or avatar_factory.DEFAULT_STYLE

    try:
        targets = await _generate_targets(sd, pool, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "_show_preview: сборка целей не удалась")
        targets = []

    if not targets:
        kb_empty = InlineKeyboardBuilder()
        kb_empty.button(text="✏️ Выбрать гео", callback_data=GeoPresenceCb(action="back_to_geo"))
        kb_empty.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
        kb_empty.adjust(1)
        await _edit(
            callback,
            "🌍 <b>Предпросмотр</b>\n\n"
            "⚠️ Не удалось собрать ни одного объекта.\n"
            "Обычно причина — не выбрана география или в списке нет городов.",
            markup=kb_empty.as_markup(),
        )
        return

    summary = infra_generator.summarize_targets(targets)

    # Аккаунты — для строки «кем создаём».
    acc_phones: list[str] = []
    if selected_acc_ids:
        try:
            acc_rows = await pool.fetch(
                "SELECT phone FROM tg_accounts WHERE id = ANY($1)", selected_acc_ids
            )
            acc_phones = [r["phone"] for r in acc_rows]
        except Exception:
            log_exc_swallow(log, "_show_preview: загрузка аккаунтов не удалась")

    geo_label = GEO_PRESETS.get(geo_preset, {}).get(
        "label", geo_preset or "Кастомный список"
    )
    estimated = estimate_duration_minutes(summary["total"])
    hours, mins = estimated // 60, estimated % 60
    duration_str = f"{hours}ч {mins}м" if hours else f"{mins}м"

    # Разбивка по типам активов — «что именно» создастся, а не общее число.
    asset_names = {"channel": "каналов", "group": "групп", "bot": "ботов"}
    breakdown = "\n".join(
        f"  • {asset_names.get(k, k)}: <b>{v}</b>"
        for k, v in sorted(summary["by_asset"].items(), key=lambda kv: -kv[1])
    )

    # Примеры — реальные цели плана, а не выдуманные образцы: те же имена
    # уйдут в исполнение, потому что генерация детерминирована по seed.
    sample_lines = []
    for t in targets[:4]:
        uname = f"@{t['planned_username']}" if t["planned_username"] else "(без username)"
        role_label = infra_generator.ROLE_LIBRARY.get(t["role"], {}).get("label", t["role"])
        sample_lines.append(
            f"  {role_label}\n"
            f"  ├ <b>{html.escape(t['planned_name'])}</b>\n"
            f"  ├ <code>{html.escape(uname)}</code>\n"
            f"  └ <i>{html.escape((t['planned_about'] or '—')[:70])}</i>"
        )
    sample_text = "\n\n".join(sample_lines)
    if summary["total"] > 4:
        sample_text += f"\n\n  <i>… и ещё {summary['total'] - 4} объектов</i>"

    accs_text = ", ".join(acc_phones[:4]) or "—"
    if len(acc_phones) > 4:
        accs_text += f" (+{len(acc_phones) - 4})"

    avatar_label = (
        "🚫 без аватаров"
        if avatar_style == "none"
        else avatar_factory.AVATAR_STYLES.get(avatar_style, {}).get("label", avatar_style)
    )

    # Честное предупреждение о целях без username: они станут приватными.
    uname_warn = ""
    if summary["missing_username"]:
        uname_warn = (
            f"\n⚠️ Без username останется {summary['missing_username']} объектов "
            f"(будут приватными)."
        )

    text = (
        f"🌍 <b>План проекта — предпросмотр</b>\n"
        f"{'─' * 28}\n"
        f"📍 Гео: {html.escape(geo_label)} — {summary['cities']} городов / "
        f"{summary['countries']} стран\n"
        f"🗺 Уровни: {_levels_line(sd)}\n"
        f"🏗 Структура: {html.escape(_structure_line(sd))}\n"
        f"📋 Шаблон оформления: {html.escape(template_name)}\n"
        f"🖼 Аватары: {avatar_label}\n"
        f"👤 Аккаунты: {html.escape(accs_text)} (round-robin)\n"
        f"⏱️ Длительность: ~{duration_str} (безопасный режим)\n"
        f"{'─' * 28}\n"
        f"<b>Будет создано: {summary['total']}</b>\n{breakdown}{uname_warn}\n"
        f"{'─' * 28}\n"
        f"<b>Примеры (реальные объекты плана):</b>\n\n{sample_text}"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=GeoPresenceCb(action="confirm"))
    kb.adjust(1)

    edit_row = InlineKeyboardBuilder()
    edit_row.button(text="🏗 Структура", callback_data=GeoPresenceCb(action="edit_roles"))
    edit_row.button(text="🗺 Уровни", callback_data=GeoPresenceCb(action="edit_levels"))
    edit_row.button(text="🖼 Аватары", callback_data=GeoPresenceCb(action="edit_avatar"))
    edit_row.button(text="🎲 Другой набор", callback_data=GeoPresenceCb(action="reroll"))
    edit_row.adjust(2, 2)
    kb.attach(edit_row)

    nav_row = InlineKeyboardBuilder()
    nav_row.button(text="✏️ Гео", callback_data=GeoPresenceCb(action="back_to_geo"))
    nav_row.button(text="📋 Шаблон", callback_data=GeoPresenceCb(action="back_to_tpl"))
    nav_row.button(text="👤 Аккаунты", callback_data=GeoPresenceCb(action="back_to_acc"))
    nav_row.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    nav_row.adjust(3, 1)
    kb.attach(nav_row)

    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(
    GeoPresenceCb.filter(F.action == "reroll"), GlobalPresenceFSM.previewing
)
async def cb_gp_reroll(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    """Пересобрать план с новым зерном — «не нравятся названия, дай другие»."""
    await safe_answer(callback)
    await state.update_data(plan_seed=random.randrange(1, 2**31))
    await _show_preview(callback, state, pool)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "edit_roles"), GlobalPresenceFSM.previewing
)
async def cb_gp_edit_roles(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.choosing_structure)
    await _show_structure_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "edit_levels"), GlobalPresenceFSM.previewing
)
async def cb_gp_edit_levels(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.choosing_levels)
    await _show_levels_step(callback, state)


@router.callback_query(
    GeoPresenceCb.filter(F.action == "edit_avatar"), GlobalPresenceFSM.previewing
)
async def cb_gp_edit_avatar(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.choosing_avatar)
    await _show_avatar_step(callback, state)




@router.callback_query(
    GeoPresenceCb.filter(F.action == "confirm"), GlobalPresenceFSM.previewing
)
async def cb_gp_confirm_preview(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
    try:
        await _cb_gp_confirm_preview_impl(callback, state, pool)
    except Exception as exc:
        log.exception("cb_gp_confirm_preview failed: %s", exc)
        try:
            await callback.message.answer(
                "⚠️ Внутренняя ошибка. Попробуйте ещё раз или нажмите ❌ Отмена."
            )
        except Exception as e:
            log.warning("handler error in cb_gp_confirm_preview: %s", e)


async def _cb_gp_confirm_preview_impl(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    sd = await state.get_data()
    geo_list: list[dict] = enrich_geo_list(sd.get("geo_list") or [])
    selected_acc_ids: list[int] = sd.get("selected_acc_ids") or []
    template_name = sd.get("template_name") or "Нет"
    geo_preset = sd.get("geo_preset", "")

    n_cities = len(geo_list)
    n_accs = len(selected_acc_ids)
    geo_label = GEO_PRESETS.get(geo_preset, {}).get(
        "label", geo_preset or "Кастомный список"
    )

    # Число объектов считается по РЕАЛЬНЫМ целям: города × тематики × уровни.
    # Раньше здесь стояло число городов — на структуре из 4 тематик экран
    # обещал вчетверо меньше объектов, чем создавалось.
    try:
        targets = await _generate_targets(sd, pool, callback.from_user.id)
    except Exception:
        log_exc_swallow(log, "_cb_gp_confirm_preview_impl: сборка целей не удалась")
        targets = []
    if not targets:
        await _edit(
            callback,
            "❌ <b>Не удалось собрать объекты плана</b>\n\n"
            "Проверьте географию и структуру.",
        )
        return

    summary = infra_generator.summarize_targets(targets)
    n_targets = summary["total"]
    asset_names = {"channel": "каналов", "group": "групп", "bot": "ботов"}
    breakdown = ", ".join(
        f"{v} {asset_names.get(k, k)}" for k, v in sorted(summary["by_asset"].items())
    )

    estimated = estimate_duration_minutes(n_targets)
    hours = estimated // 60
    mins = estimated % 60
    duration_str = f"~{hours}ч {mins}м" if hours else f"~{mins}м"

    warning = ""
    if n_targets > 20:
        warning = (
            f"\n\n⚠️ <b>Внимание:</b> будет создано {n_targets} объектов. "
            f"Это займёт значительное время. Убедитесь, что аккаунтов достаточно ({n_accs})."
        )

    # Intelligence block — fetch and decision check are separated so that
    # a failure in go_decision UI (e.g. edit_text raises) cannot swallow the block.
    intel = None
    intel_text = ""
    try:
        intel = await asyncio.wait_for(
            intelligence_engine.get_pre_launch_intelligence(
                pool,
                callback.from_user.id,
                "global_presence",
                n_targets,
                account_ids=selected_acc_ids if selected_acc_ids else None,
            ),
            timeout=30.0,
        )
        intel_text = intelligence_engine.format_pre_launch_block(intel)
    except (asyncio.TimeoutError, Exception):
        intel_text = ""

    if intel is not None and not intel.go_decision:
        await state.set_state(GlobalPresenceFSM.previewing)
        kb_err = InlineKeyboardBuilder()
        kb_err.button(
            text="◀️ Назад к предпросмотру",
            callback_data=GeoPresenceCb(action="back_to_preview"),
        )
        kb_err.button(
            text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel")
        )
        kb_err.adjust(1)
        await _edit(
            callback,
            f"🚫 <b>Запуск заблокирован</b>\n\n"
            f"{html.escape(intel.go_reason)}\n\n"
            f"Исправьте проблему и попробуйте снова.",
            markup=kb_err.as_markup(),
        )
        return

    await state.set_state(GlobalPresenceFSM.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="🚀 Запустить", callback_data=GeoPresenceCb(action="launch"))
    kb.button(text="◀️ Назад", callback_data=GeoPresenceCb(action="back_to_preview"))
    kb.button(text="❌ Отмена", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    if intel_text and len(intel_text) > 600:
        intel_text = intel_text[:597] + "…"
    intel_section = f"\n\n{intel_text}" if intel_text else ""

    # Escape user-provided strings to prevent HTML parse errors
    safe_geo_label = html.escape(geo_label)
    safe_template = html.escape(template_name)
    safe_structure = html.escape(_structure_line(sd))
    pattern_line = (sd.get("name_pattern") or "").strip()
    pattern_display = (
        f"<code>{html.escape(pattern_line)}</code>"
        if pattern_line
        else "библиотека тематик (разные названия)"
    )

    await callback.message.edit_text(
        f"🌍 <b>Финальное подтверждение</b>\n"
        f"{'─' * 28}\n\n"
        f"<b>Что будет создано:</b>\n"
        f"🏗 Структура: {safe_structure}\n"
        f"🗺 Уровни: {_levels_line(sd)}\n"
        f"📍 Гео: {safe_geo_label} ({n_cities} городов)\n"
        f"🔤 Названия: {pattern_display}\n"
        f"📋 Шаблон: {safe_template}\n"
        f"👤 Аккаунтов: {n_accs} (round-robin)\n"
        f"⏱️ ETA: {duration_str} (безопасный режим)\n\n"
        f"🔢 Итого: <b>{n_targets} объектов</b> — {breakdown}.\n"
        f"Операция запустится через очередь — вы получите уведомление о завершении."
        f"{warning}"
        f"{intel_section}\n\n"
        f"<b>Запустить?</b>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


# ── Step 8: Launch ─────────────────────────────────────────────────────────


async def _submit_plan(
    pool: asyncpg.Pool,
    owner_id: int,
    sd: dict,
    targets: list[dict],
    op_type: str,
    plan_asset_type: str,
) -> tuple[int | None, int | None]:
    """Создать план, записать цели, поставить операцию. → (plan_id, op_id).

    op_id = None означает: план в БД есть, но в очередь он не встал. Вызывающий
    обязан это показать — «тихий успех» здесь стоил бы пользователю ожидания
    операции, которой не существует.
    """
    plan_id = await db.create_global_presence_plan(
        pool,
        owner_id=owner_id,
        asset_type=plan_asset_type,
        # name_pattern — NOT NULL в схеме; при работе от библиотеки конкретного
        # паттерна нет, поэтому пишем честную пометку, а не пустую строку.
        name_pattern=(sd.get("name_pattern") or "").strip() or "[библиотека тематик]",
        username_pattern=(sd.get("username_pattern") or "").strip() or None,
        geo_selection={
            "preset": sd.get("geo_preset", ""),
            "count": len(sd.get("geo_list") or []),
        },
        account_selection={"account_ids": sd.get("selected_acc_ids") or []},
        template_id=sd.get("template_id"),
        roles=_selected_roles(sd),
        levels=_selected_levels(sd),
        name_pool=[sd["name_pattern"]] if (sd.get("name_pattern") or "").strip() else None,
        username_pool=[sd["username_pattern"]]
        if (sd.get("username_pattern") or "").strip()
        else None,
        avatar_style=(sd.get("avatar_style") or avatar_factory.DEFAULT_STYLE),
        plan_seed=sd.get("plan_seed"),
    )
    await db.create_global_presence_targets(pool, plan_id, targets)

    try:
        op_id = await operation_bus.submit(
            pool, owner_id, op_type, {"plan_id": plan_id}, total_items=len(targets)
        )
    except Exception as exc:
        log.error("global_presence: постановка операции %s не удалась: %s", op_type, exc)
        return plan_id, None

    if op_id:
        await db.link_plan_to_operation(pool, plan_id, op_id)
    return plan_id, op_id


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
    owner_id = callback.from_user.id
    asset_type = sd.get("asset_type", "channel")

    if not (sd.get("geo_list") and sd.get("selected_acc_ids")):
        log.warning(
            "global_presence launch rejected: missing data user_id=%s geo=%d accounts=%d",
            owner_id,
            len(sd.get("geo_list") or []),
            len(sd.get("selected_acc_ids") or []),
        )
        await _edit(callback, "❌ Недостаточно данных для запуска. Начните сначала.")
        await state.clear()
        return

    try:
        targets = await _generate_targets(sd, pool, owner_id)
    except Exception:
        log_exc_swallow(log, "cb_gp_launch: сборка целей не удалась")
        targets = []

    if not targets:
        await _edit(
            callback,
            "❌ <b>Не удалось собрать объекты плана</b>\n\n"
            "Проверьте географию и структуру и попробуйте снова.",
        )
        return

    # Боты создаются через BotFather отдельным исполнителем, каналы и группы —
    # общим. Поэтому план делится по этой границе, а не по каждому типу актива:
    # каналы и чаты одной структуры остаются в одной операции.
    bot_targets = [t for t in targets if t.get("asset_type") == "bot"]
    main_targets = [t for t in targets if t.get("asset_type") != "bot"]

    launched: list[tuple[str, int, int | None]] = []  # (подпись, plan_id, op_id)
    primary_plan_id: int | None = None

    if main_targets:
        plan_id, op_id = await _submit_plan(
            pool,
            owner_id,
            sd,
            main_targets,
            "global_presence_channel",
            "channel",
        )
        primary_plan_id = plan_id
        launched.append(("📡 Каналы и группы", plan_id, op_id))

    if bot_targets:
        plan_id_b, op_id_b = await _submit_plan(
            pool, owner_id, sd, bot_targets, "global_presence_bot", "bot"
        )
        if primary_plan_id is None:
            primary_plan_id = plan_id_b
        launched.append(("🤖 Боты", plan_id_b, op_id_b))

    if not launched or primary_plan_id is None:
        await _edit(
            callback,
            "❌ <b>Не удалось создать план</b>\n\nПовторите попытку.",
        )
        return

    log.info(
        "global_presence launch: user=%s asset_type=%s targets=%d (bots=%d) plans=%s",
        owner_id,
        asset_type,
        len(targets),
        len(bot_targets),
        [(p, o) for _, p, o in launched],
    )

    # Экосистема под проект — чтобы созданные активы сразу были сгруппированы,
    # а не растворились в общем списке каналов.
    try:
        from services import ecosystem_brain as _eb

        _geo_label = (
            GEO_PRESETS.get(sd.get("geo_preset", ""), {}).get("label")
            or f"{len(sd.get('geo_list') or [])} городов"
        )
        _eco_id = await _eb.create_ecosystem(
            pool,
            owner_id,
            f"GP: {_structure_line(sd)} — {_geo_label}"[:120],
            ecosystem_type="global_presence",
            region=sd.get("geo_preset") or None,
        )
        for _, _pid, _ in launched:
            await pool.execute(
                "UPDATE global_presence_plans SET ecosystem_id=$1 WHERE id=$2",
                _eco_id,
                _pid,
            )
        await _eb.record_event(
            pool,
            _eco_id,
            owner_id,
            "plan_started",
            f"GP план #{primary_plan_id} запущен ({len(targets)} объектов)",
            severity="info",
            details={"plan_id": primary_plan_id, "asset_type": asset_type},
        )
    except Exception as _eco_err:
        log.debug("global_presence: авто-создание экосистемы не удалось: %s", _eco_err)

    await state.clear()

    summary = infra_generator.summarize_targets(targets)
    asset_names = {"channel": "каналов", "group": "групп", "bot": "ботов"}
    breakdown = "\n".join(
        f"  • {asset_names.get(k, k)}: <b>{v}</b>" for k, v in sorted(summary["by_asset"].items())
    )
    queue_lines = []
    for label, plan_id_x, op_id_x in launched:
        if op_id_x:
            queue_lines.append(f"{label}: план #{plan_id_x}, операция #{op_id_x}")
        else:
            # Честно: план сохранён, но в очередь не встал — иначе пользователь
            # будет ждать выполнения, которого не начнётся.
            queue_lines.append(
                f"{label}: план #{plan_id_x} — ⚠️ <b>не встал в очередь</b>, "
                f"запустите повтор из «Мои планы»"
            )
    queue_text = "\n".join(queue_lines)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📊 Прогресс",
        callback_data=GeoPresenceCb(action="progress", plan_id=primary_plan_id),
    )
    kb.button(text="📋 Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.button(text="◀️ Назад к меню", callback_data=GeoPresenceCb(action="cancel"))
    kb.adjust(1)

    await _edit(
        callback,
        f"✅ <b>Проект запущен</b>\n"
        f"{'─' * 28}\n"
        f"Всего объектов: <b>{summary['total']}</b>\n{breakdown}\n\n"
        f"{queue_text}\n\n"
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
    await safe_answer(callback)

    try:
        stats = await db.get_global_presence_stats(
            pool, plan_id, owner_id=callback.from_user.id
        )
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
    running_now = stats.get("running", 0)

    # Percent: done out of total (includes running-in-progress items in denominator)
    pct = int(done / total * 100) if total else 0
    bar_filled = pct // 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    # Estimate remaining: pending + currently running (they haven't completed yet)
    remaining = pending + running_now
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

    running_line = f"🔄 Выполняется: {running_now}\n" if running_now > 0 else ""
    text = (
        f"🌍 <b>Global Presence Plan #{plan_id}</b>\n"
        f"Статус: {status_map.get(plan['status'], plan['status'])}\n"
        f"{'─' * 28}\n"
        f"Всего: {total}\n"
        f"✅ Создано: {done}\n"
        f"❌ Ошибок: {failed}\n"
        f"⏳ Ожидают: {pending}\n"
        f"{running_line}"
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
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await safe_answer(callback)
        from bot.keyboards import subscription_locked_markup

        await callback.message.edit_text(
            locked_text("Global Presence", "enterprise"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise", back_callback=BmCb(action="visibility")),
        )
        return
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
        reset_count = await db.reset_failed_targets(
            pool, plan_id, owner_id=callback.from_user.id
        )
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
    await safe_answer(callback)

    try:
        stats = await db.get_global_presence_stats(
            pool, plan_id, owner_id=callback.from_user.id
        )
        # Разбивка по ролям и типам — «что именно получилось», а не одно число.
        by_role = await pool.fetch(
            "SELECT COALESCE(role,'—') AS role, asset_type, "
            "       COUNT(*) FILTER (WHERE status='done')   AS done, "
            "       COUNT(*) FILTER (WHERE status='failed') AS failed, "
            "       COUNT(*) FILTER (WHERE avatar_applied)  AS with_avatar, "
            "       COUNT(*) FILTER (WHERE final_username IS NOT NULL) AS with_uname "
            "  FROM global_presence_targets WHERE plan_id=$1 "
            " GROUP BY 1,2 ORDER BY 1",
            plan_id,
        )
        done_targets = await pool.fetch(
            "SELECT city, planned_name, final_username, planned_username, "
            "       result_asset_id, avatar_applied, role "
            "  FROM global_presence_targets WHERE plan_id=$1 AND status='done' "
            " ORDER BY id LIMIT 10",
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
        by_role, done_targets, failed_targets = [], [], []

    role_lines = []
    total_avatars = total_unames = 0
    for r in by_role:
        label = infra_generator.ROLE_LIBRARY.get(r["role"], {}).get("label", r["role"])
        kind = {"channel": "канал", "group": "группа", "bot": "бот"}.get(
            r["asset_type"], r["asset_type"]
        )
        total_avatars += r["with_avatar"] or 0
        total_unames += r["with_uname"] or 0
        fail_part = f", ошибок {r['failed']}" if r["failed"] else ""
        role_lines.append(
            f"  • {html.escape(str(label))} ({kind}): создано {r['done']}{fail_part}"
        )
    role_text = "\n".join(role_lines)

    done_lines = "\n".join(
        f"  ✅ {html.escape(t['city'] or '?')}: {html.escape(t['planned_name'] or '?')}"
        # Показываем ФАКТИЧЕСКИЙ username: запланированный мог быть занят.
        + (f" → @{html.escape(t['final_username'])}" if t["final_username"] else " <i>(без username)</i>")
        + ("" if t["avatar_applied"] else " ⚠️ без аватара")
        for t in done_targets
    )
    if stats["done"] > len(done_targets):
        done_lines += f"\n  … и ещё {stats['done'] - len(done_targets)}"

    fail_lines = "\n".join(
        f"  ❌ {html.escape(t['city'] or '?')}: {html.escape((t['error_message'] or '?')[:60])}"
        for t in failed_targets[:5]
    )

    _status_ru = {
        "queued": "В очереди",
        "running": "Выполняется",
        "done": "Завершён",
        "failed": "Ошибка",
        "cancelled": "Отменён",
        "draft": "Черновик",
    }
    # Честная сводка оформления: «создано» и «оформлено» — разные числа, и
    # объект без аватара/username хуже ранжируется, поэтому это видно в отчёте.
    dressing = ""
    if stats["done"]:
        dressing = (
            f"🖼 С аватаром: {total_avatars} из {stats['done']}\n"
            f"🔗 С username: {total_unames} из {stats['done']}\n"
        )

    text = (
        f"📊 <b>Отчёт по проекту #{plan_id}</b>\n"
        f"{'─' * 28}\n"
        f"Статус: {_status_ru.get(plan['status'], plan['status'])}\n\n"
        f"📊 Всего объектов: {stats['total']}\n"
        f"✅ Создано: {stats['done']}\n"
        f"❌ Ошибок: {stats['failed']}\n"
        f"⏳ Ожидают: {stats['pending']}\n"
        f"{dressing}"
        + (f"\n<b>По тематикам:</b>\n{role_text}\n" if role_text else "")
        + (f"\n<b>Созданные объекты:</b>\n{done_lines}\n" if done_lines else "")
        + (f"\n<b>Ошибки:</b>\n{fail_lines}" if fail_lines else "")
    )

    kb = InlineKeyboardBuilder()
    if stats["done"]:
        kb.button(
            text="📥 Экспорт ссылок",
            callback_data=GeoPresenceCb(action="export", plan_id=plan_id),
        )
        kb.button(
            text="🧰 Пакетные операции",
            callback_data=GeoPresenceCb(action="bulk_menu", plan_id=plan_id),
        )
    kb.button(
        text="◀️ Прогресс",
        callback_data=GeoPresenceCb(action="progress", plan_id=plan_id),
    )
    kb.adjust(2, 1)
    await _edit(callback, text, markup=kb.as_markup())


# ── Каталог: экспорт ссылок ────────────────────────────────────────────────


@router.callback_query(GeoPresenceCb.filter(F.action == "export"))
async def cb_gp_export(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    """Выгрузка каталога объектов проекта в CSV.

    Ссылки на созданное — главный практический результат проекта; без выгрузки
    их пришлось бы переписывать с экрана вручную.
    """
    plan_id = callback_data.plan_id
    plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return
    await safe_answer(callback)

    try:
        rows = await pool.fetch(
            "SELECT t.country, t.region, t.city, t.role, t.level, t.asset_type, "
            "       t.planned_name, COALESCE(t.final_username, t.planned_username) AS uname, "
            "       t.result_asset_id, t.avatar_applied, t.status, t.error_message, "
            "       a.phone "
            "  FROM global_presence_targets t "
            "  LEFT JOIN tg_accounts a ON a.id = t.selected_account_id "
            " WHERE t.plan_id=$1 ORDER BY t.id",
            plan_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_gp_export: выборка целей не удалась")
        await callback.answer("Ошибка выгрузки", show_alert=True)
        return

    if not rows:
        await callback.answer("В плане нет объектов", show_alert=True)
        return

    import csv
    import io as _io

    buf = _io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        ["Страна", "Регион", "Город", "Тематика", "Уровень", "Тип", "Название",
         "Username", "Ссылка", "ID", "Аватар", "Статус", "Аккаунт", "Ошибка"]
    )
    for r in rows:
        uname = r["uname"] or ""
        writer.writerow(
            [
                r["country"] or "", r["region"] or "", r["city"] or "",
                infra_generator.ROLE_LIBRARY.get(r["role"], {}).get("label", r["role"] or ""),
                r["level"] or "", r["asset_type"] or "",
                r["planned_name"] or "",
                f"@{uname}" if uname else "",
                f"https://t.me/{uname}" if uname else "",
                r["result_asset_id"] or "",
                "да" if r["avatar_applied"] else "нет",
                r["status"] or "",
                r["phone"] or "",
                (r["error_message"] or "")[:200],
            ]
        )

    from aiogram.types import BufferedInputFile

    # BOM — чтобы Excel не открыл кириллицу кракозябрами.
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    n_links = sum(1 for r in rows if r["uname"] and r["status"] == "done")
    try:
        await callback.message.answer_document(
            BufferedInputFile(data, filename=f"project_{plan_id}.csv"),
            caption=(
                f"📥 <b>Каталог проекта #{plan_id}</b>\n"
                f"Объектов: {len(rows)}, публичных ссылок: {n_links}"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("cb_gp_export: отправка файла не удалась: %s", exc)
        await callback.answer("Не удалось отправить файл", show_alert=True)


# ── Каталог: пакетные операции ─────────────────────────────────────────────


@router.callback_query(GeoPresenceCb.filter(F.action == "bulk_menu"))
async def cb_gp_bulk_menu(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    plan_id = callback_data.plan_id
    plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return
    await safe_answer(callback)

    try:
        row = await pool.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE status='done' AND asset_type<>'bot') AS applicable, "
            "       COUNT(*) FILTER (WHERE status='done' AND NOT avatar_applied "
            "                        AND asset_type<>'bot') AS no_avatar "
            "  FROM global_presence_targets WHERE plan_id=$1",
            plan_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_gp_bulk_menu: подсчёт целей не удался")
        row = None
    applicable = (row["applicable"] if row else 0) or 0
    no_avatar = (row["no_avatar"] if row else 0) or 0

    kb = InlineKeyboardBuilder()
    if applicable:
        kb.button(
            text="📝 Обновить описания",
            callback_data=GeoPresenceCb(action="bulk_run", plan_id=plan_id, item="about"),
        )
        kb.button(
            text="🖼 Перерисовать аватары",
            callback_data=GeoPresenceCb(action="bulk_run", plan_id=plan_id, item="avatar"),
        )
        kb.button(
            text="🎨 Оформить всё (описание + аватар)",
            callback_data=GeoPresenceCb(action="bulk_run", plan_id=plan_id, item="both"),
        )
    kb.button(
        text="◀️ К отчёту", callback_data=GeoPresenceCb(action="report", plan_id=plan_id)
    )
    kb.adjust(2, 1, 1)

    if not applicable:
        body = (
            "В проекте пока нет созданных каналов и групп, к которым можно "
            "применить оформление."
        )
    else:
        body = (
            f"Объектов под применение: <b>{applicable}</b>\n"
            + (f"⚠️ Без аватара сейчас: {no_avatar}\n" if no_avatar else "")
            + "\n📝 <b>Обновить описания</b> — заново применит описание каждого "
            "объекта (у каждого своё, по его городу и тематике).\n"
            "🖼 <b>Перерисовать аватары</b> — новый набор картинок, по-прежнему "
            "разных.\n\n"
            "<i>Операция идёт через общую очередь: аккаунты в карантине "
            "пропускаются, прогресс и повтор — как у остальных операций.</i>"
        )

    await _edit(
        callback,
        f"🧰 <b>Пакетные операции — проект #{plan_id}</b>\n{'─' * 28}\n{body}",
        markup=kb.as_markup(),
    )


@router.callback_query(GeoPresenceCb.filter(F.action == "bulk_run"))
async def cb_gp_bulk_run(
    callback: CallbackQuery,
    callback_data: GeoPresenceCb,
    pool: asyncpg.Pool,
) -> None:
    plan_id = callback_data.plan_id
    action = callback_data.item or "both"
    if action not in ("about", "avatar", "both"):
        await callback.answer("Неизвестное действие", show_alert=True)
        return

    plan = await db.get_global_presence_plan(pool, plan_id, callback.from_user.id)
    if not plan:
        await callback.answer("План не найден", show_alert=True)
        return

    ready, reason = await infra_orchestrator.is_ready_for_op(pool, callback.from_user.id)
    if not ready:
        await callback.answer(f"🚫 {reason}", show_alert=True)
        return

    try:
        n_targets = await pool.fetchval(
            "SELECT COUNT(*) FROM global_presence_targets "
            " WHERE plan_id=$1 AND status='done' AND asset_type<>'bot'",
            plan_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_gp_bulk_run: подсчёт целей не удался")
        n_targets = 0
    if not n_targets:
        await callback.answer("Нет объектов для применения", show_alert=True)
        return

    params = {"plan_id": plan_id, "action": action}
    if action in ("avatar", "both"):
        # Сдвиг seed делает перерисовку осмысленной: без него аватары были бы
        # ровно теми же, что уже стоят.
        params["seed_shift"] = random.randrange(1, 10**6)

    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "gp_bulk_apply",
            params,
            total_items=n_targets,
        )
    except Exception as exc:
        log.error("cb_gp_bulk_run: постановка операции не удалась: %s", exc)
        await callback.answer("Не удалось поставить операцию", show_alert=True)
        return
    if not op_id:
        await callback.answer("Не удалось создать операцию", show_alert=True)
        return

    label = {
        "about": "описания",
        "avatar": "аватары",
        "both": "описания и аватары",
    }[action]
    await callback.answer(
        f"✅ Операция #{op_id} поставлена: {label} для {n_targets} объектов",
        show_alert=True,
    )
    await cb_gp_bulk_menu(callback, callback_data, pool)



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
    await safe_answer(callback)
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
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.choosing_geo)
    await _show_geo_step(callback, state)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_uname"))
async def cb_gp_back_uname(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await safe_answer(callback)
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
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.choosing_template)
    await _show_template_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_acc"))
async def cb_gp_back_acc(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.choosing_accounts)
    await _show_accounts_step(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "back_to_preview"))
async def cb_gp_back_preview(
    callback: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    await safe_answer(callback)
    await state.set_state(GlobalPresenceFSM.previewing)
    await _show_preview(callback, state, pool)


@router.callback_query(GeoPresenceCb.filter(F.action == "cancel"))
async def cb_gp_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Операции", callback_data=BmCb(action="operations"))
    kb.button(text="🌍 Мои планы", callback_data=GeoPresenceCb(action="plans_list"))
    kb.adjust(2)
    await _edit(
        callback, "❌ <b>Global Presence Factory</b> — отменено.", markup=kb.as_markup()
    )

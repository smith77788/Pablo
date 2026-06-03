"""Ecosystem Brain — управление экосистемами BotMother.

EPOCH III: Экосистема как первичный объект.
Каждая экосистема — живой объект с Health/Pressure/Risk/Memory/Drift.
"""

from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import asyncpg

from bot.callbacks import EcoCb, BmCb
from bot.states import EcosystemCreateFSM, EcosystemAddMemberFSM, EcosystemDnaFSM, EcosystemCloneFSM
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup

log = logging.getLogger(__name__)
router = Router()

_ECO_TYPES = {
    "custom":          ("🛠️", "Пользовательская"),
    "regional":        ("🌍", "Региональная"),
    "global_presence": ("🌐", "Глобальное присутствие"),
    "media_network":   ("📡", "Медиасеть"),
    "strike_network":  ("⚡", "Strike-сеть"),
}

_MEMBER_TYPES = {
    "account": ("📱", "Аккаунты"),
    "channel": ("📡", "Каналы"),
    "group":   ("👥", "Группы"),
    "bot":     ("🤖", "Боты"),
    "proxy":   ("🌐", "Прокси"),
}


async def _edit(cb: CallbackQuery, text: str, markup=None, **kw) -> None:
    await cb.answer()
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup, **kw)
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=markup, **kw)


# ── Main ecosystem list ───────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "menu"))
async def cb_eco_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await _edit(callback, locked_text("Ecosystem Brain", "starter"),
                    subscription_locked_markup("starter"))
        return

    from services import ecosystem_brain as _eb
    ecosystems = await _eb.list_ecosystems(pool, callback.from_user.id)

    kb = InlineKeyboardBuilder()
    if not ecosystems:
        text = (
            "🌐 <b>Ecosystem Brain</b>\n\n"
            "Экосистема — это живой объект, объединяющий аккаунты, каналы, "
            "группы и ботов в единую управляемую структуру.\n\n"
            "У вас ещё нет ни одной экосистемы.\n"
            "Создайте первую — и BotMother начнёт мыслить экосистемами."
        )
    else:
        lines = ["🌐 <b>Ecosystem Brain</b>\n"]
        for e in ecosystems[:10]:
            icon = _ECO_TYPES.get(e["ecosystem_type"], ("🌐", ""))[0]
            risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "🚨"}.get(
                e.get("risk_level", "low"), "🟢"
            )
            health_pct = int((e.get("health_score") or 1.0) * 100)
            cnt = e.get("member_count", 0)
            lines.append(
                f"{icon} <b>{html.escape(e['name'])}</b>  {risk_icon} {health_pct}%  "
                f"<i>{cnt} объектов</i>"
            )
            kb.button(
                text=f"{icon} {e['name'][:25]}",
                callback_data=EcoCb(action="view", eco_id=e["id"]),
            )
        kb.adjust(1)
        text = "\n".join(lines)

    kb.button(text="➕ Создать экосистему", callback_data=EcoCb(action="create"))
    if ecosystems:
        kb.button(text="📊 Сводка всех экосистем", callback_data=EcoCb(action="summary"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
    kb.adjust(1)

    await _edit(callback, text, kb.as_markup())


# ── Create wizard ─────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "create"))
async def cb_eco_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EcosystemCreateFSM.name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "🌐 <b>Новая экосистема</b>\n\n"
        "Введите название экосистемы:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.message(EcosystemCreateFSM.name)
async def fsm_eco_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Название от 1 до 64 символов. Попробуйте ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(EcosystemCreateFSM.description)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=EcoCb(action="create_skip_desc"))
    kb.button(text="❌ Отмена",    callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"📝 <b>{html.escape(name)}</b>\n\nДобавьте описание (необязательно):",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(EcoCb.filter(F.action == "create_skip_desc"))
async def cb_eco_skip_desc(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(description="")
    await _show_type_picker(callback, state)


@router.message(EcosystemCreateFSM.description)
async def fsm_eco_desc(message: Message, state: FSMContext) -> None:
    desc = (message.text or "").strip()[:256]
    await state.update_data(description=desc)
    await _show_type_picker_msg(message, state)


async def _show_type_picker(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(EcosystemCreateFSM.ecosystem_type)
    kb = InlineKeyboardBuilder()
    for key, (icon, label) in _ECO_TYPES.items():
        kb.button(text=f"{icon} {label}", callback_data=EcoCb(action=f"create_type_{key}"))
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="menu"))
    kb.adjust(2, 2, 1, 1)
    await callback.message.edit_text(
        "🏷 <b>Тип экосистемы</b>\n\nВыберите тип:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


async def _show_type_picker_msg(message: Message, state: FSMContext) -> None:
    await state.set_state(EcosystemCreateFSM.ecosystem_type)
    kb = InlineKeyboardBuilder()
    for key, (icon, label) in _ECO_TYPES.items():
        kb.button(text=f"{icon} {label}", callback_data=EcoCb(action=f"create_type_{key}"))
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="menu"))
    kb.adjust(2, 2, 1, 1)
    await message.answer(
        "🏷 <b>Тип экосистемы</b>\n\nВыберите тип:",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


@router.callback_query(EcoCb.filter(F.action.startswith("create_type_")))
async def cb_eco_create_type(
    callback: CallbackQuery, callback_data: EcoCb,
    state: FSMContext, pool: asyncpg.Pool
) -> None:
    eco_type = callback_data.action.replace("create_type_", "")
    if eco_type not in _ECO_TYPES:
        await callback.answer("Неверный тип", show_alert=True)
        return

    data = await state.get_data()
    await state.clear()

    from services import ecosystem_brain as _eb
    eco_id = await _eb.create_ecosystem(
        pool,
        owner_id=callback.from_user.id,
        name=data.get("name", "Новая экосистема"),
        description=data.get("description", ""),
        ecosystem_type=eco_type,
    )

    # Auto-discover members
    added = await _eb.auto_discover_members(pool, eco_id, callback.from_user.id)
    added_str = " | ".join(
        f"{icon} {added[t]}" for t, (icon, _) in _MEMBER_TYPES.items() if t in added
    ) or "объекты добавлены вручную"

    icon = _ECO_TYPES[eco_type][0]
    await callback.answer("✅ Экосистема создана", show_alert=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Открыть экосистему", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.button(text="📋 Все экосистемы",    callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Экосистема создана</b>\n\n"
        f"{icon} <b>{html.escape(data.get('name', ''))}</b>\n\n"
        f"Автообнаружение объектов: {added_str}",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── Ecosystem view ────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "view"))
async def cb_eco_view(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    await callback.answer("⏳ Анализирую...")

    snap = await _eb.get_snapshot(pool, eco_id, callback.from_user.id)
    if not snap:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    text = _eb.format_snapshot(snap)

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Здоровье",    callback_data=EcoCb(action="health",   eco_id=eco_id))
    kb.button(text="⚡ Давление",    callback_data=EcoCb(action="pressure", eco_id=eco_id))
    kb.button(text="⚠️ Риски",      callback_data=EcoCb(action="risk",     eco_id=eco_id))
    kb.button(text="👥 Участники",  callback_data=EcoCb(action="members",  eco_id=eco_id))
    kb.button(text="🔀 Дрейф",      callback_data=EcoCb(action="drift",    eco_id=eco_id))
    kb.button(text="📋 История",    callback_data=EcoCb(action="history",  eco_id=eco_id))
    kb.button(text="🧬 DNA",        callback_data=EcoCb(action="dna_menu", eco_id=eco_id))
    kb.button(text="♻️ Клон",       callback_data=EcoCb(action="clone_start", eco_id=eco_id))
    kb.button(text="🔄 Обновить",   callback_data=EcoCb(action="view",     eco_id=eco_id))
    kb.button(text="🔃 Синхр.",     callback_data=EcoCb(action="sync",     eco_id=eco_id))
    kb.button(text="◀️ Назад",      callback_data=EcoCb(action="menu"))
    kb.adjust(3, 2, 2, 2, 2)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Health detail ─────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "health"))
async def cb_eco_health(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    await callback.answer()
    health = await _eb.compute_health(pool, eco_id, callback.from_user.id)

    bar_h  = _eb.format_health_bar(health.health_score)
    bar_s  = _eb.format_health_bar(health.stability_score)
    bar_r  = _eb.format_health_bar(health.reliability_score)
    bar_rc = _eb.format_health_bar(health.recovery_score)
    bar_g  = _eb.format_health_bar(health.growth_score)

    text = (
        f"📊 <b>Здоровье: {html.escape(eco['name'])}</b>\n\n"
        f"<b>Итог:</b> {health.grade}\n\n"
        f"🏥 Здоровье       [{bar_h}] {health.health_score:.0%}\n"
        f"🔒 Стабильность   [{bar_s}] {health.stability_score:.0%}\n"
        f"⚙️ Надёжность     [{bar_r}] {health.reliability_score:.0%}\n"
        f"🔄 Восстановление [{bar_rc}] {health.recovery_score:.0%}\n"
        f"📈 Рост           [{bar_g}] {health.growth_score:.0%}\n\n"
        f"📱 Аккаунты: {health.healthy_accounts}/{health.account_count} готовы\n"
    )
    if health.restrictions_count:
        text += f"⛔ Ограничений: {health.restrictions_count}\n"
    if health.active_proxies:
        text += f"🌐 Прокси: {health.healthy_proxies}/{health.active_proxies} активны\n"
    text += f"\n📋 Успешность операций: {health.recent_op_success_rate:.0%}"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=EcoCb(action="health", eco_id=eco_id))
    kb.button(text="◀️ Назад",   callback_data=EcoCb(action="view",   eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Pressure detail ───────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "pressure"))
async def cb_eco_pressure(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    await callback.answer()
    p = await _eb.compute_pressure(pool, eco_id, callback.from_user.id)
    bar = _eb.format_health_bar(p.score / 100)

    text = (
        f"⚡ <b>Давление: {html.escape(eco['name'])}</b>\n\n"
        f"[{bar}] <b>{p.score}/100</b>  {p.level}\n\n"
        f"📱 Кулдаун-аккаунты: {p.cooldown_ratio:.0%}\n"
        f"📊 Плотность операций: {p.operation_density:.1f} ops/аккаунт\n"
        f"⚙️ Активных задач: {p.active_tasks}\n"
    )
    if p.overloaded_accounts:
        text += f"⛔ Перегруженных аккаунтов: {p.overloaded_accounts}\n"
    if p.overloaded_proxies:
        text += f"🌐 Проблемных прокси: {p.overloaded_proxies}\n"

    if p.score >= 85:
        text += "\n🚫 <b>Давление критическое.</b> Операции могут быть заблокированы."
    elif p.score >= 70:
        text += "\n⚠️ <b>Высокое давление.</b> Рекомендуется снизить нагрузку."

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=EcoCb(action="pressure", eco_id=eco_id))
    kb.button(text="◀️ Назад",   callback_data=EcoCb(action="view",     eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Risk detail ───────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "risk"))
async def cb_eco_risk(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    await callback.answer()
    risk = await _eb.compute_risk(pool, eco_id, callback.from_user.id)

    bar_o  = _eb.format_health_bar(1.0 - risk.operational_risk)
    bar_i  = _eb.format_health_bar(1.0 - risk.infrastructure_risk)
    bar_a  = _eb.format_health_bar(1.0 - risk.account_risk)
    bar_p  = _eb.format_health_bar(1.0 - risk.proxy_risk)
    bar_rc = _eb.format_health_bar(1.0 - risk.recovery_risk)

    text = (
        f"⚠️ <b>Риски: {html.escape(eco['name'])}</b>\n\n"
        f"<b>Уровень риска:</b> {risk.level_label}\n\n"
        f"⚙️ Операционный     [{bar_o}] {risk.operational_risk:.0%}\n"
        f"🏗 Инфраструктурный [{bar_i}] {risk.infrastructure_risk:.0%}\n"
        f"📱 Аккаунтов        [{bar_a}] {risk.account_risk:.0%}\n"
        f"🌐 Прокси           [{bar_p}] {risk.proxy_risk:.0%}\n"
        f"🔄 Восстановления   [{bar_rc}] {risk.recovery_risk:.0%}\n"
    )
    if risk.reasons:
        text += "\n<b>Причины:</b>\n" + _eb.format_risk_reasons(risk)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=EcoCb(action="risk",  eco_id=eco_id))
    kb.button(text="◀️ Назад",   callback_data=EcoCb(action="view",  eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Members ───────────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "members"))
async def cb_eco_members(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    await callback.answer()
    members = await _eb.get_members(pool, eco_id)

    lines = [f"👥 <b>Участники: {html.escape(eco['name'])}</b>\n"]
    by_type: dict[str, list] = {}
    for m in members:
        by_type.setdefault(m["object_type"], []).append(m)

    for obj_type, (icon, label) in _MEMBER_TYPES.items():
        if obj_type in by_type:
            lines.append(f"{icon} <b>{label}:</b> {len(by_type[obj_type])}")

    if not members:
        lines.append("Участников нет. Добавьте объекты в экосистему.")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Автообнаружение", callback_data=EcoCb(action="autodiscover", eco_id=eco_id))
    kb.button(text="🗑 Очистить",        callback_data=EcoCb(action="members_clear", eco_id=eco_id))
    kb.button(text="🔄 Обновить",        callback_data=EcoCb(action="members",      eco_id=eco_id))
    kb.button(text="◀️ Назад",          callback_data=EcoCb(action="view",         eco_id=eco_id))
    kb.adjust(2, 2)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(EcoCb.filter(F.action == "autodiscover"))
async def cb_eco_autodiscover(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    await callback.answer("⏳ Обнаруживаю объекты...")

    added = await _eb.auto_discover_members(pool, eco_id, callback.from_user.id)
    if added:
        added_str = " | ".join(
            f"{icon} +{added[t]}" for t, (icon, _) in _MEMBER_TYPES.items() if t in added
        )
        await callback.answer(f"✅ Добавлено: {added_str}", show_alert=True)
    else:
        await callback.answer("Новых объектов не найдено", show_alert=True)

    # Refresh members view
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    members = await _eb.get_members(pool, eco_id)
    lines = [f"👥 <b>Участники: {html.escape(eco['name'])}</b>\n"]
    by_type: dict[str, list] = {}
    for m in members:
        by_type.setdefault(m["object_type"], []).append(m)
    for obj_type, (icon, label) in _MEMBER_TYPES.items():
        if obj_type in by_type:
            lines.append(f"{icon} <b>{label}:</b> {len(by_type[obj_type])}")
    if not members:
        lines.append("Участников нет.")

    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Автообнаружение", callback_data=EcoCb(action="autodiscover",   eco_id=eco_id))
    kb.button(text="🗑 Очистить",        callback_data=EcoCb(action="members_clear",  eco_id=eco_id))
    kb.button(text="🔄 Обновить",        callback_data=EcoCb(action="members",        eco_id=eco_id))
    kb.button(text="◀️ Назад",          callback_data=EcoCb(action="view",           eco_id=eco_id))
    kb.adjust(2, 2)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(EcoCb.filter(F.action == "members_clear"))
async def cb_eco_members_clear(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    eco_id = callback_data.eco_id
    await pool.execute(
        "DELETE FROM ecosystem_members WHERE ecosystem_id=$1 AND owner_id=$2",
        eco_id, callback.from_user.id,
    )
    await callback.answer("✅ Все участники удалены", show_alert=True)
    from services import ecosystem_brain as _eb
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Автообнаружение", callback_data=EcoCb(action="autodiscover", eco_id=eco_id))
    kb.button(text="◀️ Назад",          callback_data=EcoCb(action="view",         eco_id=eco_id))
    kb.adjust(1)
    name = eco["name"] if eco else "?"
    await callback.message.edit_text(
        f"👥 <b>Участники очищены</b>\n\n<i>{html.escape(name)}</i> — участников нет.",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── Drift ─────────────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "drift"))
async def cb_eco_drift(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    await callback.answer("⏳ Анализирую дрейф...")
    drifts = await _eb.detect_drift(pool, eco_id, callback.from_user.id)

    if not drifts:
        text = (
            f"🔀 <b>Дрейф: {html.escape(eco['name'])}</b>\n\n"
            "✅ Отклонений не обнаружено.\n"
            "Экосистема соответствует стандартам."
        )
    else:
        lines = [f"🔀 <b>Дрейф: {html.escape(eco['name'])}</b>\n"]
        type_labels = {
            "resource_gap":       "🔴 Нехватка ресурса",
            "config_deviation":   "🟠 Отклонение конфига",
            "template_mismatch":  "🟡 Несоответствие шаблону",
            "sync_loss":          "🔴 Потеря синхронизации",
        }
        for d in drifts[:5]:
            label = type_labels.get(d.drift_type, "⚠️ Дрейф")
            lines.append(f"\n{label}")
            lines.append(f"  {html.escape(d.description)}")
            if d.suggested_fix:
                lines.append(f"  💡 {html.escape(d.suggested_fix)}")
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Повторить анализ", callback_data=EcoCb(action="drift",  eco_id=eco_id))
    kb.button(text="◀️ Назад",           callback_data=EcoCb(action="view",   eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── History ───────────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "history"))
async def cb_eco_history(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    await callback.answer()
    events = await pool.fetch(
        """SELECT event_type, severity, title, occurred_at
           FROM ecosystem_events
           WHERE ecosystem_id=$1
           ORDER BY occurred_at DESC LIMIT 20""",
        eco_id,
    )

    if not events:
        text = (
            f"📋 <b>История: {html.escape(eco['name'])}</b>\n\n"
            "Событий пока нет."
        )
    else:
        import datetime as _dt
        sev_icons = {"info": "ℹ️", "warning": "⚠️", "error": "🔴", "critical": "🚨"}
        lines = [f"📋 <b>История: {html.escape(eco['name'])}</b>\n"]
        for ev in events:
            icon = sev_icons.get(ev["severity"], "ℹ️")
            ts = ev["occurred_at"]
            if hasattr(ts, "strftime"):
                ts_str = ts.strftime("%d.%m %H:%M")
            else:
                ts_str = str(ts)[:16]
            lines.append(f"{icon} <b>{html.escape(ev['title'])}</b>  <i>{ts_str}</i>")
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Summary all ecosystems ────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "summary"))
async def cb_eco_summary(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    from services import ecosystem_brain as _eb
    await callback.answer("⏳ Считаю сводку...")

    ecosystems = await _eb.list_ecosystems(pool, callback.from_user.id)
    if not ecosystems:
        await callback.answer("Экосистем нет", show_alert=True)
        return

    lines = ["🌐 <b>Сводка всех экосистем</b>\n"]
    total_health = 0.0
    for e in ecosystems[:10]:
        icon = _ECO_TYPES.get(e["ecosystem_type"], ("🌐", ""))[0]
        risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "🚨"}.get(
            e.get("risk_level", "low"), "🟢"
        )
        h = int((e.get("health_score") or 1.0) * 100)
        total_health += h
        pressure = e.get("pressure_score", 0)
        lines.append(
            f"{icon} <b>{html.escape(e['name'])}</b>\n"
            f"   Здоровье: {h}%  Давление: {pressure}  {risk_icon}"
        )
    avg_health = int(total_health / len(ecosystems))
    lines.append(f"\n<b>Средн. здоровье:</b> {avg_health}%  •  Экосистем: {len(ecosystems)}")

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Archive ───────────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "archive"))
async def cb_eco_archive(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    await _eb.delete_ecosystem(pool, callback_data.eco_id, callback.from_user.id)
    await callback.answer("✅ Экосистема архивирована", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Все экосистемы", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "📦 <b>Экосистема архивирована</b>",
        parse_mode="HTML", reply_markup=kb.as_markup(),
    )


# ── Ecosystem Copilot snooze ──────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "eco_snooze"))
async def cb_eco_snooze(
    callback: CallbackQuery, callback_data: EcoCb
) -> None:
    from services import ecosystem_copilot as _ec
    hours = callback_data.page or 1
    _ec.snooze_ecosystem_alerts(callback.from_user.id, float(hours))

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Снять снуз",    callback_data=EcoCb(action="eco_snooze_clear"))
    kb.button(text="🌐 Экосистемы",    callback_data=EcoCb(action="menu"))
    kb.adjust(2)

    await callback.answer(f"😴 Уведомления отложены на {hours}ч", show_alert=False)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception:
        pass


@router.callback_query(EcoCb.filter(F.action == "eco_snooze_clear"))
async def cb_eco_snooze_clear(callback: CallbackQuery) -> None:
    from services import ecosystem_copilot as _ec
    _ec._snooze_until.pop(callback.from_user.id, None)
    await callback.answer("✅ Уведомления возобновлены", show_alert=False)

    kb = InlineKeyboardBuilder()
    kb.button(text="😴 1ч",         callback_data=EcoCb(action="eco_snooze", page=1))
    kb.button(text="😴 6ч",         callback_data=EcoCb(action="eco_snooze", page=6))
    kb.button(text="😴 24ч",        callback_data=EcoCb(action="eco_snooze", page=24))
    kb.button(text="🌐 Экосистемы", callback_data=EcoCb(action="menu"))
    kb.adjust(3, 1)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception:
        pass


# ── Sync scores ───────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "sync"))
async def cb_eco_sync(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    await callback.answer("⏳ Синхронизирую метрики…")
    try:
        result = await _eb.sync_ecosystem_scores(pool, eco_id, callback.from_user.id)
        h_pct  = int(result["health"] * 100)
        p      = result["pressure"]
        risk   = result["risk_level"]
        risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "🚨"}.get(risk, "🟢")
        text = (
            f"🔃 <b>Метрики синхронизированы</b>\n\n"
            f"🏥 Здоровье: <b>{h_pct}%</b>\n"
            f"⚡ Давление: <b>{p}/100</b>\n"
            f"{risk_icon} Риск: <b>{risk}</b>\n"
        )
    except Exception as e:
        text = f"❌ Ошибка синхронизации: {html.escape(str(e)[:100])}"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад к экосистеме", callback_data=EcoCb(action="view", eco_id=eco_id))
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Clone ─────────────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "clone_start"))
async def cb_eco_clone_start(
    callback: CallbackQuery, callback_data: EcoCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return
    await callback.answer()
    await state.update_data(clone_source_id=eco_id)
    await state.set_state(EcosystemCloneFSM.naming)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="view", eco_id=eco_id))
    await _edit(callback,
        f"♻️ <b>Клонирование экосистемы</b>\n\n"
        f"Источник: <b>{html.escape(eco['name'])}</b>\n\n"
        f"Введите название для новой (клонированной) экосистемы:",
        markup=kb.as_markup(),
    )


@router.message(EcosystemCloneFSM.naming)
async def fsm_eco_clone_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    from services import ecosystem_brain as _eb
    sd = await state.get_data()
    source_id = sd.get("clone_source_id", 0)
    new_name = (message.text or "").strip()[:80]
    if not new_name:
        await message.answer("Название не может быть пустым. Введите название клона:")
        return

    await state.clear()
    try:
        new_id = await _eb.clone_ecosystem(pool, source_id, message.from_user.id, new_name)
        kb = InlineKeyboardBuilder()
        kb.button(text="🌐 Открыть клон", callback_data=EcoCb(action="view", eco_id=new_id))
        kb.button(text="📋 Все экосистемы", callback_data=EcoCb(action="menu"))
        kb.adjust(1)
        await message.answer(
            f"✅ <b>Экосистема клонирована</b>\n\n"
            f"Новая экосистема: <b>{html.escape(new_name)}</b> (#{new_id})\n"
            f"Все участники скопированы.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка клонирования: {html.escape(str(e)[:100])}",
                             parse_mode="HTML")


# ── DNA Templates ─────────────────────────────────────────────────────────────

@router.callback_query(EcoCb.filter(F.action == "dna_menu"))
async def cb_eco_dna_menu(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    dna_list = await _eb.list_dna(pool, callback.from_user.id)

    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Снять DNA с этой экосистемы",
              callback_data=EcoCb(action="dna_capture", eco_id=eco_id))
    for d in dna_list[:8]:
        is_mine = d["owner_id"] == callback.from_user.id
        label = ("💾 " if is_mine else "📚 ") + html.escape(d["name"][:28])
        kb.button(text=label, callback_data=EcoCb(action="dna_view", eco_id=eco_id, page=d["id"]))
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)

    lines = ["🧬 <b>DNA-шаблоны экосистемы</b>\n"]
    if dna_list:
        for d in dna_list[:8]:
            is_mine = d["owner_id"] == callback.from_user.id
            flag = "💾" if is_mine else "📚"
            lines.append(f"{flag} <b>{html.escape(d['name'])}</b> · {d['dna_type']}")
    else:
        lines.append("Нет сохранённых DNA-шаблонов.\nСнимите DNA с текущей экосистемы — и она станет шаблоном для клонов.")

    await _edit(callback, "\n".join(lines), markup=kb.as_markup())


@router.callback_query(EcoCb.filter(F.action == "dna_capture"))
async def cb_eco_dna_capture(
    callback: CallbackQuery, callback_data: EcoCb,
    state: FSMContext, pool: asyncpg.Pool,
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return
    await callback.answer()
    await state.update_data(dna_source_eco_id=eco_id)
    await state.set_state(EcosystemDnaFSM.naming)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="dna_menu", eco_id=eco_id))
    await _edit(callback,
        f"📸 <b>Снятие DNA-шаблона</b>\n\n"
        f"Экосистема: <b>{html.escape(eco['name'])}</b>\n\n"
        f"Введите название для DNA-шаблона:",
        markup=kb.as_markup(),
    )


@router.message(EcosystemDnaFSM.naming)
async def fsm_eco_dna_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    from services import ecosystem_brain as _eb
    sd = await state.get_data()
    eco_id = sd.get("dna_source_eco_id", 0)
    name = (message.text or "").strip()[:80]
    if not name:
        await message.answer("Название не может быть пустым. Введите название шаблона:")
        return

    await state.clear()
    try:
        dna_id = await _eb.capture_dna_from_ecosystem(pool, eco_id, message.from_user.id, name)
        kb = InlineKeyboardBuilder()
        kb.button(text="🧬 DNA-шаблоны", callback_data=EcoCb(action="dna_menu", eco_id=eco_id))
        kb.button(text="◀️ Экосистема",  callback_data=EcoCb(action="view",     eco_id=eco_id))
        kb.adjust(1)
        await message.answer(
            f"✅ <b>DNA снята</b>\n\n"
            f"Шаблон <b>{html.escape(name)}</b> (#{dna_id}) сохранён.\n"
            f"Его можно применить к любой другой экосистеме.",
            parse_mode="HTML", reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {html.escape(str(e)[:100])}", parse_mode="HTML")


@router.callback_query(EcoCb.filter(F.action == "dna_view"))
async def cb_eco_dna_view(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    import json as _json
    eco_id = callback_data.eco_id
    dna_id = callback_data.page  # reuse page field as dna_id
    dna = await _eb.get_dna(pool, dna_id, callback.from_user.id)
    if not dna:
        await callback.answer("DNA не найдена", show_alert=True)
        return
    await callback.answer()

    td = dna.get("template_data") or {}
    if isinstance(td, str):
        try:
            td = _json.loads(td)
        except Exception:
            td = {}

    mc = td.get("member_counts") or {}
    mc_lines = " | ".join(f"{k}: {v}" for k, v in mc.items()) if mc else "нет данных"
    is_mine = dna["owner_id"] == callback.from_user.id

    text = (
        f"🧬 <b>DNA: {html.escape(dna['name'])}</b>\n\n"
        f"Тип: {dna['dna_type']}\n"
        f"Описание: {html.escape(dna.get('description', '—'))}\n"
        f"Состав: {mc_lines}\n"
        f"Публичная: {'да' if dna.get('is_public') else 'нет'}\n"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Применить к экосистеме",
              callback_data=EcoCb(action="dna_apply", eco_id=eco_id, page=dna_id))
    if is_mine:
        kb.button(text="🗑 Удалить DNA",
                  callback_data=EcoCb(action="dna_delete", eco_id=eco_id, page=dna_id))
    kb.button(text="◀️ Назад к DNA", callback_data=EcoCb(action="dna_menu", eco_id=eco_id))
    kb.adjust(1)
    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(EcoCb.filter(F.action == "dna_apply"))
async def cb_eco_dna_apply(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    dna_id = callback_data.page
    await callback.answer("⏳ Применяю DNA…")
    try:
        changes = await _eb.apply_dna_to_ecosystem(pool, dna_id, eco_id, callback.from_user.id)
        ch_lines = "\n".join(f"• {k}: {v}" for k, v in changes.items()) or "нет изменений"
        text = f"✅ <b>DNA применена</b>\n\n{ch_lines}"
    except Exception as e:
        text = f"❌ Ошибка: {html.escape(str(e)[:100])}"

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад к экосистеме", callback_data=EcoCb(action="view", eco_id=eco_id))
    await _edit(callback, text, markup=kb.as_markup())


@router.callback_query(EcoCb.filter(F.action == "dna_delete"))
async def cb_eco_dna_delete(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb
    eco_id = callback_data.eco_id
    dna_id = callback_data.page
    await _eb.delete_dna(pool, dna_id, callback.from_user.id)
    await callback.answer("🗑 DNA удалена", show_alert=True)
    # Show updated DNA menu directly
    dna_list = await _eb.list_dna(pool, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Снять DNA с этой экосистемы",
              callback_data=EcoCb(action="dna_capture", eco_id=eco_id))
    for d in dna_list[:8]:
        is_mine = d["owner_id"] == callback.from_user.id
        label = ("💾 " if is_mine else "📚 ") + html.escape(d["name"][:28])
        kb.button(text=label, callback_data=EcoCb(action="dna_view", eco_id=eco_id, page=d["id"]))
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)
    lines = ["🧬 <b>DNA-шаблоны экосистемы</b>\n"]
    if dna_list:
        for d in dna_list[:8]:
            flag = "💾" if d["owner_id"] == callback.from_user.id else "📚"
            lines.append(f"{flag} <b>{html.escape(d['name'])}</b> · {d['dna_type']}")
    else:
        lines.append("Нет сохранённых DNA-шаблонов.")
    try:
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML",
                                         reply_markup=kb.as_markup())
    except Exception:
        pass


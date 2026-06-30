"""Ecosystem Brain — управление экосистемами Infragram.

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

from bot.callbacks import (
    EcoCb,
    BmCb,
    EcoPickCb,
    GeoPresenceCb,
    ChanFactCb,
    GroupFCb,
    BotFactCb,
)
from bot.states import EcosystemCreateFSM, EcosystemDnaFSM, EcosystemCloneFSM
from bot.utils.subscription import require_plan, locked_text
from bot.keyboards import subscription_locked_markup
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router()

_ECO_TYPES = {
    "custom": ("🛠️", "Пользовательская"),
    "regional": ("🌍", "Региональная"),
    "global_presence": ("🌐", "Глобальное присутствие"),
    "media_network": ("📡", "Медиасеть"),
    "strike_network": ("⚡", "Strike-сеть"),
}

_MEMBER_TYPES = {
    "account": ("📱", "Аккаунты"),
    "channel": ("📡", "Каналы"),
    "group": ("👥", "Группы"),
    "bot": ("🤖", "Боты"),
    "proxy": ("🌐", "Прокси"),
}

_DNA_TYPES = {
    "regional": ("🌍", "Региональная", "Регион, геопресеты, локальные настройки"),
    "publishing": ("📝", "Публикации", "Шаблоны постов, связанные каналы"),
    "visibility": ("👁", "Видимость", "Снимок Health/Stability/аккаунтов"),
    "custom": ("🛠️", "Универсальная", "Полный слепок экосистемы"),
}


async def _edit(cb: CallbackQuery, text: str, markup=None, **kw) -> None:
    await cb.answer()
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=markup, **kw)
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" in err_str:
            return
        if "there is no text in the message to edit" in err_str:
            try:
                await cb.message.edit_caption(caption=text, parse_mode="HTML", reply_markup=markup)
                return
            except Exception:
                pass
        if "message to edit not found" in err_str or "message can't be edited" in err_str:
            await cb.bot.send_message(cb.from_user.id, text, parse_mode="HTML", reply_markup=markup, **kw)
        else:
            log.warning("ecosystems _edit error: %s", e)


# ── Main ecosystem list ───────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "menu"))
async def cb_eco_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await _edit(
            callback,
            locked_text("Ecosystem Brain", "starter"),
            subscription_locked_markup("starter", back_callback=BmCb(action="assets")),
        )
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
            "Создайте первую — и Infragram начнёт мыслить экосистемами."
        )
    else:
        total_health = sum((e.get("health_score") or 1.0) for e in ecosystems)
        avg_h = int(total_health / len(ecosystems) * 100)
        total_objs = sum(e.get("member_count", 0) for e in ecosystems)
        critical = sum(
            1 for e in ecosystems if e.get("risk_level") in ("high", "critical")
        )
        summary_icon = "🟢" if avg_h >= 70 else ("🟡" if avg_h >= 40 else "🔴")

        lines = [
            "🌐 <b>Ecosystem Brain</b>\n",
            f"{summary_icon} Здоровье: <b>{avg_h}%</b>  •  "
            f"Экосистем: <b>{len(ecosystems)}</b>  •  "
            f"Объектов: <b>{total_objs}</b>",
        ]
        if critical:
            lines.append(f"🚨 Требуют внимания: <b>{critical}</b>")
        lines.append("")
        for e in ecosystems[:10]:
            icon = _ECO_TYPES.get(e["ecosystem_type"], ("🌐", ""))[0]
            risk_icon = {
                "low": "🟢",
                "medium": "🟡",
                "high": "🔴",
                "critical": "🚨",
            }.get(e.get("risk_level", "low"), "🟢")
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
        kb.button(
            text="📊 Сводка всех экосистем", callback_data=EcoCb(action="summary")
        )
    kb.button(text="◀️ Назад", callback_data=BmCb(action="assets"))
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
        "🌐 <b>Новая экосистема</b>\n\nВведите название экосистемы:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
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
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"📝 <b>{html.escape(name)}</b>\n\nДобавьте описание (необязательно):",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
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
        kb.button(
            text=f"{icon} {label}", callback_data=EcoCb(action=f"create_type_{key}")
        )
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="menu"))
    kb.adjust(2, 2, 1, 1)
    await callback.message.edit_text(
        "🏷 <b>Тип экосистемы</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


async def _show_type_picker_msg(message: Message, state: FSMContext) -> None:
    await state.set_state(EcosystemCreateFSM.ecosystem_type)
    kb = InlineKeyboardBuilder()
    for key, (icon, label) in _ECO_TYPES.items():
        kb.button(
            text=f"{icon} {label}", callback_data=EcoCb(action=f"create_type_{key}")
        )
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="menu"))
    kb.adjust(2, 2, 1, 1)
    await message.answer(
        "🏷 <b>Тип экосистемы</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(EcoCb.filter(F.action.startswith("create_type_")))
async def cb_eco_create_type(
    callback: CallbackQuery, callback_data: EcoCb, state: FSMContext, pool: asyncpg.Pool
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
    added_str = (
        " | ".join(
            f"{icon} {added[t]}" for t, (icon, _) in _MEMBER_TYPES.items() if t in added
        )
        or "объекты добавлены вручную"
    )

    icon = _ECO_TYPES[eco_type][0]
    await callback.answer("✅ Экосистема создана", show_alert=False)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔍 Открыть экосистему", callback_data=EcoCb(action="view", eco_id=eco_id)
    )
    kb.button(text="📋 Все экосистемы", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"✅ <b>Экосистема создана</b>\n\n"
        f"{icon} <b>{html.escape(data.get('name', ''))}</b>\n\n"
        f"Автообнаружение объектов: {added_str}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Ecosystem view ────────────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "view"))
async def cb_eco_view(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await state.clear()
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    await callback.answer("⏳ Анализирую...")

    snap = await _eb.get_snapshot(pool, eco_id, callback.from_user.id)
    if not snap:
        kb_back = InlineKeyboardBuilder()
        kb_back.button(text="◀️ Все экосистемы", callback_data=EcoCb(action="menu"))
        try:
            await callback.message.edit_text(
                "❌ Экосистема не найдена.",
                parse_mode="HTML",
                reply_markup=kb_back.as_markup(),
            )
        except Exception:
            pass
        return

    text = _eb.format_snapshot(snap)

    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Здоровье", callback_data=EcoCb(action="health", eco_id=eco_id))
    kb.button(text="⚡ Давление", callback_data=EcoCb(action="pressure", eco_id=eco_id))
    kb.button(text="⚠️ Риски", callback_data=EcoCb(action="risk", eco_id=eco_id))
    kb.button(text="👥 Участники", callback_data=EcoCb(action="members", eco_id=eco_id))
    kb.button(text="🔀 Дрейф", callback_data=EcoCb(action="drift", eco_id=eco_id))
    kb.button(text="📋 История", callback_data=EcoCb(action="history", eco_id=eco_id))
    kb.button(text="💡 Рекомендации", callback_data=EcoCb(action="recs", eco_id=eco_id))
    kb.button(text="🔃 Синхр.", callback_data=EcoCb(action="sync", eco_id=eco_id))
    kb.button(text="🧬 DNA", callback_data=EcoCb(action="dna_menu", eco_id=eco_id))
    kb.button(text="♻️ Клон", callback_data=EcoCb(action="clone_start", eco_id=eco_id))
    kb.button(text="🏭 Фабрика", callback_data=EcoCb(action="factory", eco_id=eco_id))
    kb.button(text="🌍 Global Presence", callback_data=GeoPresenceCb(action="menu"))
    kb.button(text="🔄 Обновить", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="menu"))
    kb.adjust(3, 2, 2, 2, 2, 1, 2)
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Ecosystem Factory Hub ────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "factory"))
async def cb_eco_factory(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    """Экосистема-Фабрика: создать активы и автоматически добавить в экосистему."""
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return
    await callback.answer()

    kb = InlineKeyboardBuilder()
    kb.button(text="📡 Создать канал", callback_data=ChanFactCb(action="create"))
    kb.button(text="👥 Создать группу", callback_data=GroupFCb(action="create"))
    kb.button(text="🤖 Добавить бота", callback_data=BotFactCb(action="import_tokens"))
    kb.button(text="🌍 Global Presence", callback_data=GeoPresenceCb(action="menu"))
    kb.button(
        text="🔍 Автообнаружение",
        callback_data=EcoCb(action="autodiscover", eco_id=eco_id),
    )
    kb.button(
        text="◀️ Назад к экосистеме", callback_data=EcoCb(action="view", eco_id=eco_id)
    )
    kb.adjust(2, 2, 1, 1)

    text = (
        f"🏭 <b>Ecosystem Factory: {html.escape(eco['name'])}</b>\n\n"
        f"Создайте активы и добавьте их в эту экосистему.\n\n"
        f"<b>📡 Каналы</b> — создать новый канал → после создания нажмите "
        f"«🌐 Добавить в экосистему»\n"
        f"<b>👥 Группы</b> — создать группу/сообщество\n"
        f"<b>🤖 Боты</b> — импортировать токен бота\n"
        f"<b>🌍 Global Presence</b> — пакетное развёртывание по гео\n"
        f"<b>🔍 Автообнаружение</b> — добавить все ваши активы автоматически"
    )
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as _e:
        _es = str(_e).lower()
        if "message to edit not found" in _es or "message can't be edited" in _es:
            await callback.bot.send_message(callback.from_user.id, text, parse_mode="HTML", reply_markup=kb.as_markup())
        elif "message is not modified" not in _es:
            log.warning("ecosystems edit error: %s", _e)


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

    bar_h = _eb.format_health_bar(health.health_score)
    bar_s = _eb.format_health_bar(health.stability_score)
    bar_r = _eb.format_health_bar(health.reliability_score)
    bar_rc = _eb.format_health_bar(health.recovery_score)
    bar_g = _eb.format_health_bar(health.growth_score)

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
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


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
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


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

    bar_o = _eb.format_health_bar(1.0 - risk.operational_risk)
    bar_i = _eb.format_health_bar(1.0 - risk.infrastructure_risk)
    bar_a = _eb.format_health_bar(1.0 - risk.account_risk)
    bar_p = _eb.format_health_bar(1.0 - risk.proxy_risk)
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
    kb.button(text="🔄 Обновить", callback_data=EcoCb(action="risk", eco_id=eco_id))
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Members ───────────────────────────────────────────────────────────────────


async def _fetch_member_names(
    pool: asyncpg.Pool, by_type: dict, owner_id: int
) -> dict[str, list[str]]:
    """Возвращает {object_type: [label, ...]} с именами участников."""
    result: dict[str, list[str]] = {}

    if "channel" in by_type or "group" in by_type:
        chan_ids = [
            m["object_id"]
            for m in by_type.get("channel", []) + by_type.get("group", [])
        ]
        if chan_ids:
            try:
                rows = await pool.fetch(
                    "SELECT channel_id, title, username FROM managed_channels "
                    "WHERE owner_id=$1 AND channel_id = ANY($2::bigint[])",
                    owner_id,
                    chan_ids,
                )
            except Exception:
                log_exc_swallow(log, "_fetch_member_names: channel fetch failed")
                rows = []
            name_map = {
                r["channel_id"]: r["title"]
                or f"@{r['username']}"
                or str(r["channel_id"])
                for r in rows
            }
            for t in ("channel", "group"):
                if t in by_type:
                    result[t] = [
                        html.escape(
                            name_map.get(m["object_id"], f"id:{m['object_id']}")
                        )
                        for m in by_type[t]
                    ]

    if "bot" in by_type:
        bot_ids = [m["object_id"] for m in by_type["bot"]]
        try:
            rows = await pool.fetch(
                "SELECT bot_id, username, first_name FROM managed_bots "
                "WHERE added_by=$1 AND bot_id = ANY($2::bigint[])",
                owner_id,
                bot_ids,
            )
        except Exception:
            log_exc_swallow(log, "_fetch_member_names: bot fetch failed")
            rows = []
        name_map = {
            r["bot_id"]: f"@{r['username']}"
            if r["username"]
            else r["first_name"] or str(r["bot_id"])
            for r in rows
        }
        result["bot"] = [
            html.escape(name_map.get(m["object_id"], f"id:{m['object_id']}"))
            for m in by_type["bot"]
        ]

    if "account" in by_type:
        acc_ids = [m["object_id"] for m in by_type["account"]]
        try:
            rows = await pool.fetch(
                "SELECT id, phone, first_name FROM tg_accounts "
                "WHERE owner_id=$1 AND id = ANY($2::int[])",
                owner_id,
                acc_ids,
            )
        except Exception:
            log_exc_swallow(log, "_fetch_member_names: account fetch failed")
            rows = []
        name_map = {
            r["id"]: r["first_name"] or r["phone"] or str(r["id"]) for r in rows
        }
        result["account"] = [
            html.escape(name_map.get(m["object_id"], f"id:{m['object_id']}"))
            for m in by_type["account"]
        ]

    if "proxy" in by_type:
        proxy_ids = [m["object_id"] for m in by_type["proxy"]]
        try:
            rows = await pool.fetch(
                "SELECT id, proxy_url FROM user_proxies WHERE id = ANY($2::int[]) AND owner_id=$1",
                owner_id,
                proxy_ids,
            )
        except Exception:
            log_exc_swallow(log, "_fetch_member_names: proxy fetch failed")
            rows = []
        name_map = {r["id"]: (r["proxy_url"] or str(r["id"]))[:40] for r in rows}
        result["proxy"] = [
            html.escape(name_map.get(m["object_id"], f"id:{m['object_id']}"))
            for m in by_type["proxy"]
        ]

    return result


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

    if not members:
        lines.append("Участников нет. Добавьте объекты в экосистему.")
    else:
        names = await _fetch_member_names(pool, by_type, callback.from_user.id)
        _MAX_SHOW = 8
        for obj_type, (icon, label) in _MEMBER_TYPES.items():
            if obj_type not in by_type:
                continue
            count = len(by_type[obj_type])
            type_names = names.get(obj_type, [])
            if not type_names:
                lines.append(f"{icon} <b>{label}:</b> {count}")
            elif count <= _MAX_SHOW:
                lines.append(f"\n{icon} <b>{label} ({count}):</b>")
                for n in type_names:
                    lines.append(f"  • {n}")
            else:
                lines.append(f"\n{icon} <b>{label} ({count}):</b>")
                for n in type_names[:_MAX_SHOW]:
                    lines.append(f"  • {n}")
                lines.append(f"  <i>...и ещё {count - _MAX_SHOW}</i>")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔍 Автообнаружение",
        callback_data=EcoCb(action="autodiscover", eco_id=eco_id),
    )
    kb.button(
        text="🗑 Очистить", callback_data=EcoCb(action="members_clear", eco_id=eco_id)
    )
    kb.button(text="🔄 Обновить", callback_data=EcoCb(action="members", eco_id=eco_id))
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
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

    # Refresh members view
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="main"))
        await callback.message.edit_text("❌ Экосистема не найдена.", reply_markup=kb.as_markup())
        return
    members = await _eb.get_members(pool, eco_id)
    if added:
        added_str = " | ".join(
            f"{icon} +{added[t]}"
            for t, (icon, _) in _MEMBER_TYPES.items()
            if t in added
        )
        lines = [
            f"👥 <b>Участники: {html.escape(eco['name'])}</b>\n✅ Добавлено: {added_str}\n"
        ]
    else:
        lines = [
            f"👥 <b>Участники: {html.escape(eco['name'])}</b>\nℹ️ Новых объектов не найдено.\n"
        ]
    by_type: dict[str, list] = {}
    for m in members:
        by_type.setdefault(m["object_type"], []).append(m)
    for obj_type, (icon, label) in _MEMBER_TYPES.items():
        if obj_type in by_type:
            lines.append(f"{icon} <b>{label}:</b> {len(by_type[obj_type])}")
    if not members:
        lines.append("Участников нет.")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔍 Автообнаружение",
        callback_data=EcoCb(action="autodiscover", eco_id=eco_id),
    )
    kb.button(
        text="🗑 Очистить", callback_data=EcoCb(action="members_clear", eco_id=eco_id)
    )
    kb.button(text="🔄 Обновить", callback_data=EcoCb(action="members", eco_id=eco_id))
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(2, 2)
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(EcoCb.filter(F.action == "members_clear"))
async def cb_eco_members_clear(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    eco_id = callback_data.eco_id
    try:
        await pool.execute(
            "DELETE FROM ecosystem_members WHERE ecosystem_id=$1 AND owner_id=$2",
            eco_id,
            callback.from_user.id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_members_clear: execute failed")
    await callback.answer("✅ Все участники удалены", show_alert=True)
    from services import ecosystem_brain as _eb

    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔍 Автообнаружение",
        callback_data=EcoCb(action="autodiscover", eco_id=eco_id),
    )
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)
    name = eco["name"] if eco else "?"
    await callback.message.edit_text(
        f"👥 <b>Участники очищены</b>\n\n<i>{html.escape(name)}</i> — участников нет.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
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
            "resource_gap": "🔴 Нехватка ресурса",
            "config_deviation": "🟠 Отклонение конфига",
            "template_mismatch": "🟡 Несоответствие шаблону",
            "sync_loss": "🔴 Потеря синхронизации",
        }
        for d in drifts[:5]:
            label = type_labels.get(d.drift_type, "⚠️ Дрейф")
            lines.append(f"\n{label}")
            lines.append(f"  {html.escape(d.description)}")
            if d.suggested_fix:
                lines.append(f"  💡 {html.escape(d.suggested_fix)}")
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔄 Повторить анализ", callback_data=EcoCb(action="drift", eco_id=eco_id)
    )
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


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
    try:
        events = await pool.fetch(
            """SELECT event_type, severity, title, occurred_at
               FROM ecosystem_events
               WHERE ecosystem_id=$1
               ORDER BY occurred_at DESC LIMIT 20""",
            eco_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_history: fetch failed")
        events = []

    if not events:
        text = f"📋 <b>История: {html.escape(eco['name'])}</b>\n\nСобытий пока нет."
    else:
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

    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Summary all ecosystems ────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "summary"))
async def cb_eco_summary(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    from services import ecosystem_brain as _eb

    await callback.answer("⏳ Считаю сводку...")

    ecosystems = await _eb.list_ecosystems(pool, callback.from_user.id)
    if not ecosystems:
        kb_back = InlineKeyboardBuilder()
        kb_back.button(text="◀️ Назад", callback_data=EcoCb(action="menu"))
        try:
            await callback.message.edit_text(
                "📊 Экосистем нет. Создайте первую в разделе 🌐 Ecosystem Brain.",
                reply_markup=kb_back.as_markup(),
            )
        except Exception:
            pass
        return

    lines = ["🌐 <b>Сводка всех экосистем</b>\n"]
    total_health = 0.0
    fetched = ecosystems[:10]
    for e in fetched:
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
    avg_health = int(total_health / len(fetched))
    lines.append(
        f"\n<b>Средн. здоровье:</b> {avg_health}%  •  Экосистем: {len(ecosystems)}"
    )

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
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Ecosystem Sync Engine ─────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "sync_preview"))
async def cb_eco_sync_preview(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return
    await callback.answer("⏳ Анализирую...")

    owner_id = callback.from_user.id

    try:
        new_accounts = (
            await pool.fetchval(
                """SELECT COUNT(*) FROM tg_accounts
               WHERE owner_id=$1 AND is_active=TRUE
                 AND NOT EXISTS (
                     SELECT 1 FROM ecosystem_members em
                     WHERE em.ecosystem_id=$2 AND em.object_type='account' AND em.object_id=tg_accounts.id
                 )""",
                owner_id,
                eco_id,
            )
            or 0
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_sync_preview: new_accounts fetchval failed")
        new_accounts = 0

    try:
        new_channels = (
            await pool.fetchval(
                """SELECT COUNT(*) FROM managed_channels mc
               WHERE mc.owner_id=$1
                 AND NOT EXISTS (
                     SELECT 1 FROM ecosystem_members em
                     WHERE em.ecosystem_id=$2 AND em.object_type='channel' AND em.object_id=mc.channel_id
                 )""",
                owner_id,
                eco_id,
            )
            or 0
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_sync_preview: new_channels fetchval failed")
        new_channels = 0

    try:
        stale_ids = await pool.fetch(
            """SELECT em.id FROM ecosystem_members em
               LEFT JOIN tg_accounts a ON a.id=em.object_id AND a.is_active=TRUE AND a.owner_id=$2
               WHERE em.ecosystem_id=$1 AND em.object_type='account' AND a.id IS NULL""",
            eco_id,
            owner_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_sync_preview: stale_ids fetch failed")
        stale_ids = []
    stale_count = len(stale_ids)

    try:
        current_count = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM ecosystem_members WHERE ecosystem_id=$1",
                eco_id,
            )
            or 0
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_sync_preview: current_count fetchval failed")
        current_count = 0

    lines = [
        f"🔁 <b>Синхронизация: {html.escape(eco['name'])}</b>\n",
        f"📦 Текущих объектов: <b>{current_count}</b>\n",
    ]
    if new_accounts or new_channels:
        lines.append("➕ <b>Доступно для добавления:</b>")
        if new_accounts:
            lines.append(f"  📱 Аккаунтов: {new_accounts}")
        if new_channels:
            lines.append(f"  📡 Каналов: {new_channels}")
        lines.append("")
    if stale_count:
        lines.append(f"🗑 Устаревших записей: {stale_count} (аккаунты отключены)")
        lines.append("")
    if not new_accounts and not new_channels and not stale_count:
        lines.append("✅ Экосистема синхронизирована. Изменений не найдено.")

    kb = InlineKeyboardBuilder()
    if new_accounts or new_channels or stale_count:
        kb.button(
            text="⚡ Выполнить синхронизацию",
            callback_data=EcoCb(action="sync_execute", eco_id=eco_id),
        )
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


@router.callback_query(EcoCb.filter(F.action == "sync_execute"))
async def cb_eco_sync_execute(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    owner_id = callback.from_user.id
    eco = await _eb.get_ecosystem(pool, eco_id, owner_id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return
    await callback.answer("⏳ Синхронизирую...")

    # Remove stale account members (account disabled/deleted)
    try:
        stale_ids = await pool.fetch(
            """SELECT em.id FROM ecosystem_members em
               LEFT JOIN tg_accounts a ON a.id=em.object_id AND a.is_active=TRUE AND a.owner_id=$2
               WHERE em.ecosystem_id=$1 AND em.object_type='account' AND a.id IS NULL""",
            eco_id,
            owner_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_sync_execute: stale_ids fetch failed")
        stale_ids = []
    if stale_ids:
        try:
            await pool.execute(
                "DELETE FROM ecosystem_members WHERE id=ANY($1::bigint[])",
                [r["id"] for r in stale_ids],
            )
        except Exception:
            log_exc_swallow(log, "cb_eco_sync_execute: delete stale execute failed")

    # Add new members via auto-discover
    added = await _eb.auto_discover_members(pool, eco_id, owner_id)
    added_total = sum(added.values())

    await _eb.record_event(
        pool,
        eco_id,
        owner_id,
        event_type="sync",
        title=f"Синхронизация: +{added_total} добавлено, {len(stale_ids)} удалено",
        severity="info",
        details={"added": added, "stale_removed": len(stale_ids)},
    )

    lines = ["✅ <b>Синхронизация выполнена</b>\n"]
    if added:
        parts = []
        for t, (icon, _) in _MEMBER_TYPES.items():
            if t in added:
                parts.append(f"{icon} +{added[t]}")
        lines.append(f"➕ Добавлено: {' | '.join(parts)}")
    if stale_ids:
        lines.append(f"🗑 Удалено устаревших: {len(stale_ids)}")
    if not added and not stale_ids:
        lines.append("Изменений не было.")

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔁 Повторить проверку",
        callback_data=EcoCb(action="sync_preview", eco_id=eco_id),
    )
    kb.button(text="◀️ Экосистема", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(2)

    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── DNA Templates ─────────────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "dna_save"))
async def cb_eco_dna_save(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    import json as _json
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    owner_id = callback.from_user.id
    eco = await _eb.get_ecosystem(pool, eco_id, owner_id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return
    await callback.answer("⏳ Сохраняю ДНК...")

    try:
        counts_rows = await pool.fetch(
            "SELECT object_type, COUNT(*) AS cnt FROM ecosystem_members WHERE ecosystem_id=$1 GROUP BY object_type",
            eco_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_dna_save: counts_rows fetch failed")
        counts_rows = []
    member_counts = {r["object_type"]: r["cnt"] for r in counts_rows}

    template_data = {
        "member_counts": member_counts,
        "ecosystem_type": eco["ecosystem_type"],
        "health_threshold": 0.65,
        "pressure_threshold": 70,
        "region": eco.get("region") or "",
    }

    try:
        dna_id = await pool.fetchval(
            """INSERT INTO ecosystem_dna (owner_id, name, dna_type, description, template_data)
               VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id""",
            owner_id,
            eco["name"] + " — ДНК",
            eco["ecosystem_type"],
            f"Шаблон из экосистемы {eco['name']}",
            _json.dumps(template_data, ensure_ascii=False),
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_dna_save: dna INSERT failed")
        await callback.answer("❌ Ошибка сохранения ДНК", show_alert=True)
        return

    try:
        await pool.execute(
            "UPDATE ecosystems SET dna_id=$1 WHERE id=$2 AND owner_id=$3",
            dna_id,
            eco_id,
            owner_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_dna_save: update dna_id execute failed")

    await _eb.record_event(
        pool,
        eco_id,
        owner_id,
        event_type="dna_saved",
        title=f"ДНК-шаблон сохранён (id={dna_id})",
        severity="info",
        details={"dna_id": dna_id, "member_counts": member_counts},
    )

    counts_str = (
        "\n".join(
            f"  {_MEMBER_TYPES.get(t, ('•', t))[0]} {t}: {c}"
            for t, c in member_counts.items()
        )
        or "  (участников нет)"
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Все ДНК-шаблоны", callback_data=EcoCb(action="dna_list"))
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(2)
    await callback.message.edit_text(
        f"🧬 <b>ДНК-шаблон сохранён</b>\n\n"
        f"<b>{html.escape(eco['name'])}</b>\n\n"
        f"Состав:\n{counts_str}\n\n"
        f"<i>Шаблон можно применить при создании новой экосистемы.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(EcoCb.filter(F.action == "dna_list"))
async def cb_eco_dna_list(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    import json as _json

    await callback.answer()
    owner_id = callback.from_user.id

    try:
        templates = await pool.fetch(
            """SELECT id, name, dna_type, template_data, created_at
               FROM ecosystem_dna WHERE owner_id=$1 ORDER BY created_at DESC LIMIT 10""",
            owner_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_dna_list: fetch failed")
        templates = []

    if not templates:
        text = (
            "🧬 <b>ДНК-шаблоны экосистем</b>\n\n"
            "Сохранённых шаблонов нет.\n"
            "Откройте экосистему → 🧬 ДНК-шаблон, чтобы создать первый."
        )
    else:
        lines = ["🧬 <b>ДНК-шаблоны экосистем</b>\n"]
        for t in templates:
            raw = t["template_data"]
            td = _json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            counts = td.get("member_counts", {})
            parts = []
            for k, v in counts.items():
                if v:
                    icon = _MEMBER_TYPES.get(k, ("•", k))[0]
                    parts.append(f"{icon}{v}")
            eco_icon = _ECO_TYPES.get(t["dna_type"], ("🌐", ""))[0]
            counts_str = " ".join(parts) if parts else "пусто"
            lines.append(
                f"{eco_icon} <b>{html.escape(t['name'])}</b>  <i>{counts_str}</i>"
            )
        text = "\n".join(lines)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb.as_markup()
    )


# ── Ecosystem Copilot snooze ──────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "eco_snooze"))
async def cb_eco_snooze(callback: CallbackQuery, callback_data: EcoCb) -> None:
    from services import ecosystem_copilot as _ec

    hours = callback_data.page or 1
    _ec.snooze_ecosystem_alerts(callback.from_user.id, float(hours))

    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Снять снуз", callback_data=EcoCb(action="eco_snooze_clear"))
    kb.button(text="🌐 Экосистемы", callback_data=EcoCb(action="menu"))
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
    kb.button(text="😴 1ч", callback_data=EcoCb(action="eco_snooze", page=1))
    kb.button(text="😴 6ч", callback_data=EcoCb(action="eco_snooze", page=6))
    kb.button(text="😴 24ч", callback_data=EcoCb(action="eco_snooze", page=24))
    kb.button(text="🌐 Экосистемы", callback_data=EcoCb(action="menu"))
    kb.adjust(3, 1)
    try:
        await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception:
        pass


# ── Sync: Preview ────────────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "sync"))
async def cb_eco_sync(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    """Шаг 1: Preview — что будет синхронизировано."""
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    await callback.answer("⏳ Анализирую состав…")

    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    try:
        counts_rows = await pool.fetch(
            "SELECT object_type, COUNT(*) AS cnt FROM ecosystem_members "
            "WHERE ecosystem_id=$1 GROUP BY object_type",
            eco_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_sync: counts_rows fetch failed")
        counts_rows = []
    counts = {r["object_type"]: r["cnt"] for r in counts_rows}
    total = sum(counts.values())

    try:
        last_sync = await pool.fetchrow(
            "SELECT occurred_at FROM ecosystem_events "
            "WHERE ecosystem_id=$1 AND event_type='sync' "
            "ORDER BY occurred_at DESC LIMIT 1",
            eco_id,
        )
    except Exception:
        log_exc_swallow(log, "cb_eco_sync: last_sync fetchrow failed")
        last_sync = None
    last_sync_str = "никогда"
    if last_sync:
        ts = last_sync["occurred_at"]
        last_sync_str = (
            ts.strftime("%d.%m %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
        )

    lines = [
        f"🔃 <b>Синхронизация: {html.escape(eco['name'])}</b>\n",
        f"Будет проверено: <b>{total} объектов</b>",
    ]
    for otype, (icon, label) in _MEMBER_TYPES.items():
        if otype in counts:
            lines.append(f"  {icon} {label}: {counts[otype]}")
    lines.extend(
        [
            f"\nПоследняя синхр.: <i>{last_sync_str}</i>",
            "\n<b>Что будет сделано:</b>",
            "• Проверка активности аккаунтов и прокси",
            "• Удаление удалённых / заблокированных объектов",
            "• Пересчёт Health · Pressure · Risk",
        ]
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="✅ Запустить синхронизацию",
        callback_data=EcoCb(action="sync_exec", eco_id=eco_id),
    )
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as _e:
        _es = str(_e).lower()
        if "message to edit not found" in _es or "message can't be edited" in _es:
            await callback.bot.send_message(callback.from_user.id, "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
        elif "message is not modified" not in _es:
            log.warning("ecosystems sync edit error: %s", _e)


# ── Sync: Execute ─────────────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "sync_exec"))
async def cb_eco_sync_exec(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    """Шаг 2: выполняет синхронизацию и показывает отчёт."""
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    await callback.answer("⏳ Синхронизирую участников и метрики…")
    try:
        diff = await _eb.sync_ecosystem_members(pool, eco_id, callback.from_user.id)
        scores = await _eb.sync_ecosystem_scores(pool, eco_id, callback.from_user.id)

        h_pct = int(scores["health"] * 100)
        risk = scores["risk_level"]
        risk_icon = {"low": "🟢", "medium": "🟡", "high": "🔴", "critical": "🚨"}.get(
            risk, "🟢"
        )

        lines = [
            "✅ <b>Синхронизация выполнена</b>\n",
            f"🏥 Здоровье: <b>{h_pct}%</b>  "
            f"⚡ Давление: <b>{scores['pressure']}/100</b>  "
            f"{risk_icon} Риск: <b>{risk}</b>",
            "",
            f"✅ Участников OK: <b>{diff['ok']}</b>",
        ]
        if diff["removed"]:
            lines.append(f"🗑 Удалено (не найдено): <b>{diff['removed']}</b>")
        if diff["stale"]:
            lines.append(f"⚠️ Проблемных: <b>{len(diff['stale'])}</b>")
            for s in diff["stale"][:4]:
                lines.append(
                    f"  • {html.escape(str(s.get('label', '?'))[:30])}: "
                    f"{html.escape(str(s.get('issue', '?')))}"
                )
        text = "\n".join(lines)
    except Exception as e:
        text = f"❌ Ошибка синхронизации: {html.escape(str(e)[:100])}"

    kb = InlineKeyboardBuilder()
    kb.button(text="💡 Рекомендации", callback_data=EcoCb(action="recs", eco_id=eco_id))
    kb.button(text="◀️ К экосистеме", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as _e:
        _es = str(_e).lower()
        if "message to edit not found" in _es or "message can't be edited" in _es:
            await callback.bot.send_message(callback.from_user.id, text, parse_mode="HTML", reply_markup=kb.as_markup())
        elif "message is not modified" not in _es:
            log.warning("ecosystems health edit error: %s", _e)


# ── Clone ─────────────────────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "clone_start"))
async def cb_eco_clone_start(
    callback: CallbackQuery,
    callback_data: EcoCb,
    state: FSMContext,
    pool: asyncpg.Pool,
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
    await _edit(
        callback,
        f"♻️ <b>Клонирование экосистемы</b>\n\n"
        f"Источник: <b>{html.escape(eco['name'])}</b>\n\n"
        f"Введите название для новой (клонированной) экосистемы:",
        markup=kb.as_markup(),
    )


@router.message(EcosystemCloneFSM.naming)
async def fsm_eco_clone_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    new_name = (message.text or "").strip()[:80]
    if not new_name:
        await message.answer("Название не может быть пустым. Введите название клона:")
        return
    await state.update_data(clone_new_name=new_name)
    await state.set_state(EcosystemCloneFSM.region)

    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data=EcoCb(action="clone_skip_region"))
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"🌍 <b>Адаптация региона</b>\n\n"
        f"Клон: <b>{html.escape(new_name)}</b>\n\n"
        f"Введите новый регион для адаптации (например: <i>Санкт-Петербург</i>)\n"
        f"или нажмите Пропустить:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(EcoCb.filter(F.action == "clone_skip_region"))
async def cb_eco_clone_skip_region(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb

    sd = await state.get_data()
    source_id = sd.get("clone_source_id", 0)
    new_name = sd.get("clone_new_name", "Клон")
    await state.clear()
    await callback.answer()
    try:
        new_id = await _eb.clone_ecosystem(
            pool, source_id, callback.from_user.id, new_name
        )
        kb = InlineKeyboardBuilder()
        kb.button(
            text="🌐 Открыть клон", callback_data=EcoCb(action="view", eco_id=new_id)
        )
        kb.button(text="📋 Все экосистемы", callback_data=EcoCb(action="menu"))
        kb.adjust(1)
        await callback.message.edit_text(
            f"✅ <b>Экосистема клонирована</b>\n\n"
            f"<b>{html.escape(new_name)}</b> (#{new_id})\n"
            f"Все участники скопированы.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка: {html.escape(str(e)[:100])}", parse_mode="HTML"
        )


@router.message(EcosystemCloneFSM.region)
async def fsm_eco_clone_region(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb

    sd = await state.get_data()
    source_id = sd.get("clone_source_id", 0)
    new_name = sd.get("clone_new_name", "Клон")
    new_region = (message.text or "").strip()[:64]
    await state.clear()
    try:
        new_id = await _eb.clone_ecosystem(
            pool, source_id, message.from_user.id, new_name
        )
        if new_region:
            await pool.execute(
                "UPDATE ecosystems SET region=$1, updated_at=now() WHERE id=$2 AND owner_id=$3",
                new_region,
                new_id,
                message.from_user.id,
            )
        kb = InlineKeyboardBuilder()
        kb.button(
            text="🌐 Открыть клон", callback_data=EcoCb(action="view", eco_id=new_id)
        )
        kb.button(text="📋 Все экосистемы", callback_data=EcoCb(action="menu"))
        kb.adjust(1)
        region_note = (
            f"Регион: <b>{html.escape(new_region)}</b>\n" if new_region else ""
        )
        await message.answer(
            f"✅ <b>Экосистема клонирована</b>\n\n"
            f"<b>{html.escape(new_name)}</b> (#{new_id})\n"
            f"{region_note}"
            f"Все участники скопированы.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await message.answer(
            f"❌ Ошибка клонирования: {html.escape(str(e)[:100])}", parse_mode="HTML"
        )


# ── DNA Templates ─────────────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "dna_menu"))
async def cb_eco_dna_menu(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await state.clear()
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    dna_list = await _eb.list_dna(pool, callback.from_user.id)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="📸 Снять DNA с этой экосистемы",
        callback_data=EcoCb(action="dna_capture", eco_id=eco_id),
    )
    for d in dna_list[:8]:
        is_mine = d["owner_id"] == callback.from_user.id
        label = ("💾 " if is_mine else "📚 ") + html.escape(d["name"][:28])
        kb.button(
            text=label,
            callback_data=EcoCb(action="dna_view", eco_id=eco_id, page=d["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=EcoCb(action="view", eco_id=eco_id))
    kb.adjust(1)

    lines = ["🧬 <b>DNA-шаблоны экосистемы</b>\n"]
    if dna_list:
        for d in dna_list[:8]:
            is_mine = d["owner_id"] == callback.from_user.id
            flag = "💾" if is_mine else "📚"
            lines.append(f"{flag} <b>{html.escape(d['name'])}</b> · {d['dna_type']}")
    else:
        lines.append(
            "Нет сохранённых DNA-шаблонов.\nСнимите DNA с текущей экосистемы — и она станет шаблоном для клонов."
        )

    await _edit(callback, "\n".join(lines), markup=kb.as_markup())


@router.callback_query(EcoCb.filter(F.action == "dna_capture"))
async def cb_eco_dna_capture(
    callback: CallbackQuery,
    callback_data: EcoCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return
    await callback.answer()
    await state.update_data(dna_source_eco_id=eco_id)

    kb = InlineKeyboardBuilder()
    for dtype, (icon, label, hint) in _DNA_TYPES.items():
        kb.button(
            text=f"{icon} {label}",
            callback_data=EcoCb(action=f"dna_type_{dtype}", eco_id=eco_id),
        )
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="dna_menu", eco_id=eco_id))
    kb.adjust(2, 2, 1)

    lines = [
        "🧬 <b>Тип DNA-шаблона</b>\n",
        f"Экосистема: <b>{html.escape(eco['name'])}</b>\n",
    ]
    for dtype, (icon, label, hint) in _DNA_TYPES.items():
        lines.append(f"{icon} <b>{label}</b> — {hint}")
    await _edit(callback, "\n".join(lines), markup=kb.as_markup())


@router.callback_query(EcoCb.filter(F.action.startswith("dna_type_")))
async def cb_eco_dna_type(
    callback: CallbackQuery,
    callback_data: EcoCb,
    state: FSMContext,
) -> None:
    dtype = callback_data.action[len("dna_type_") :]
    if dtype not in _DNA_TYPES:
        await callback.answer("Неверный тип", show_alert=True)
        return
    eco_id = callback_data.eco_id
    await state.update_data(dna_type=dtype)
    await state.set_state(EcosystemDnaFSM.naming)

    icon, label, _ = _DNA_TYPES[dtype]
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=EcoCb(action="dna_menu", eco_id=eco_id))
    await _edit(
        callback,
        f"📸 <b>{icon} DNA: {label}</b>\n\nВведите название для шаблона:",
        markup=kb.as_markup(),
    )


@router.message(EcosystemDnaFSM.naming)
async def fsm_eco_dna_name(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb

    sd = await state.get_data()
    eco_id = sd.get("dna_source_eco_id", 0)
    dna_type = sd.get("dna_type", "custom")
    name = (message.text or "").strip()[:80]
    if not name:
        await message.answer("Название не может быть пустым. Введите название шаблона:")
        return

    await state.clear()
    icon = _DNA_TYPES.get(dna_type, ("🛠️", "", ""))[0]
    try:
        dna_id = await _eb.capture_dna_from_ecosystem(
            pool, eco_id, message.from_user.id, name, dna_type=dna_type
        )
        kb = InlineKeyboardBuilder()
        kb.button(
            text="🧬 DNA-шаблоны", callback_data=EcoCb(action="dna_menu", eco_id=eco_id)
        )
        kb.button(
            text="◀️ Экосистема", callback_data=EcoCb(action="view", eco_id=eco_id)
        )
        kb.adjust(1)
        await message.answer(
            f"✅ <b>DNA снята</b>\n\n"
            f"{icon} Шаблон <b>{html.escape(name)}</b> (#{dna_id}) сохранён.\n"
            f"Тип: {dna_type} · применим к любой экосистеме.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        await message.answer(
            f"❌ Ошибка: {html.escape(str(e)[:100])}", parse_mode="HTML"
        )


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
    kb.button(
        text="✅ Применить к экосистеме",
        callback_data=EcoCb(action="dna_apply", eco_id=eco_id, page=dna_id),
    )
    if is_mine:
        kb.button(
            text="🗑 Удалить DNA",
            callback_data=EcoCb(action="dna_delete", eco_id=eco_id, page=dna_id),
        )
    kb.button(
        text="◀️ Назад к DNA", callback_data=EcoCb(action="dna_menu", eco_id=eco_id)
    )
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
        changes = await _eb.apply_dna_to_ecosystem(
            pool, dna_id, eco_id, callback.from_user.id
        )
        ch_lines = (
            "\n".join(f"• {k}: {v}" for k, v in changes.items()) or "нет изменений"
        )
        text = f"✅ <b>DNA применена</b>\n\n{ch_lines}"
    except Exception as e:
        text = f"❌ Ошибка: {html.escape(str(e)[:100])}"

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ Назад к экосистеме", callback_data=EcoCb(action="view", eco_id=eco_id)
    )
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
    kb.button(
        text="📸 Снять DNA с этой экосистемы",
        callback_data=EcoCb(action="dna_capture", eco_id=eco_id),
    )
    for d in dna_list[:8]:
        is_mine = d["owner_id"] == callback.from_user.id
        label = ("💾 " if is_mine else "📚 ") + html.escape(d["name"][:28])
        kb.button(
            text=label,
            callback_data=EcoCb(action="dna_view", eco_id=eco_id, page=d["id"]),
        )
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
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception:
        pass


# ── Recommendations ───────────────────────────────────────────────────────────


@router.callback_query(EcoCb.filter(F.action == "recs"))
async def cb_eco_recs(
    callback: CallbackQuery, callback_data: EcoCb, pool: asyncpg.Pool
) -> None:
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    await callback.answer("⏳ Анализирую…")

    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        kb_back = InlineKeyboardBuilder()
        kb_back.button(text="◀️ Все экосистемы", callback_data=EcoCb(action="menu"))
        try:
            await callback.message.edit_text(
                "❌ Экосистема не найдена.", reply_markup=kb_back.as_markup()
            )
        except Exception:
            pass
        return

    recs = await _eb.generate_recommendations(pool, eco_id, callback.from_user.id)

    lines = [f"💡 <b>Рекомендации: {html.escape(eco['name'])}</b>\n"]
    _priority_labels = {"high": "🔴 Критично", "medium": "🟡 Важно", "low": "🟢 Совет"}
    for r in recs:
        priority_label = _priority_labels.get(r["priority"], "")
        lines.append(
            f"{r['icon']} <b>{html.escape(r['title'])}</b>  <i>{priority_label}</i>"
        )
        lines.append(f"  ↳ {html.escape(r['action'])}")
    if len(recs) > 1:
        high_count = sum(1 for r in recs if r["priority"] == "high")
        if high_count:
            lines.append(
                f"\n🚨 Критичных: {high_count} — требуют немедленного внимания"
            )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔃 Синхронизировать", callback_data=EcoCb(action="sync", eco_id=eco_id)
    )
    kb.button(text="🔀 Дрейф", callback_data=EcoCb(action="drift", eco_id=eco_id))
    kb.button(
        text="◀️ Назад к экосистеме", callback_data=EcoCb(action="view", eco_id=eco_id)
    )
    kb.adjust(2, 1)
    try:
        await callback.message.edit_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as _e:
        _es = str(_e).lower()
        if "message to edit not found" in _es or "message can't be edited" in _es:
            await callback.bot.send_message(callback.from_user.id, "\n".join(lines), parse_mode="HTML", reply_markup=kb.as_markup())
        elif "message is not modified" not in _es:
            log.warning("ecosystems recs edit error: %s", _e)


# ── EcoPickCb: добавить объект в экосистему (из фабрик) ──────────────────────


@router.callback_query(EcoPickCb.filter(F.action == "list"))
async def cb_ecopick_list(
    callback: CallbackQuery, callback_data: EcoPickCb, pool: asyncpg.Pool
) -> None:
    """Показывает список экосистем для добавления объекта."""
    from services import ecosystem_brain as _eb

    object_type = callback_data.object_type
    object_id = callback_data.object_id
    await callback.answer()

    ecosystems = await _eb.list_ecosystems(pool, callback.from_user.id)

    _type_labels = {
        "channel": "канал",
        "group": "группу",
        "bot": "бот",
        "account": "аккаунт",
        "proxy": "прокси",
    }
    obj_label = _type_labels.get(object_type, object_type)

    kb = InlineKeyboardBuilder()
    if not ecosystems:
        text = (
            f"🌐 <b>Добавить {obj_label} в экосистему</b>\n\n"
            f"У вас нет активных экосистем.\n"
            f"Создайте экосистему в разделе 🌐 Ecosystem Brain."
        )
        kb.button(text="🌐 Создать экосистему", callback_data=EcoCb(action="create"))
    else:
        text = (
            f"🌐 <b>Выберите экосистему</b>\n\n"
            f"Куда добавить {obj_label} (ID: {object_id})?"
        )
        for e in ecosystems[:10]:
            icon = {
                "global_presence": "🌐",
                "regional": "🌍",
                "media_network": "📡",
                "strike_network": "⚡",
            }.get(e["ecosystem_type"], "🌐")
            h_pct = int((e.get("health_score") or 1.0) * 100)
            kb.button(
                text=f"{icon} {html.escape(e['name'][:25])} ({h_pct}%)",
                callback_data=EcoPickCb(
                    action="add",
                    object_type=object_type,
                    object_id=object_id,
                    eco_id=e["id"],
                ),
            )
    kb.button(text="❌ Пропустить", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb.as_markup()
        )
    except Exception as _e:
        _es = str(_e).lower()
        if "message to edit not found" in _es or "message can't be edited" in _es:
            await callback.bot.send_message(callback.from_user.id, text, parse_mode="HTML", reply_markup=kb.as_markup())
        elif "message is not modified" not in _es:
            log.warning("ecosystems autodiscover edit error: %s", _e)


@router.callback_query(EcoPickCb.filter(F.action == "add"))
async def cb_ecopick_add(
    callback: CallbackQuery, callback_data: EcoPickCb, pool: asyncpg.Pool
) -> None:
    """Добавляет объект в выбранную экосистему."""
    from services import ecosystem_brain as _eb

    eco_id = callback_data.eco_id
    object_type = callback_data.object_type
    object_id = callback_data.object_id

    eco = await _eb.get_ecosystem(pool, eco_id, callback.from_user.id)
    if not eco:
        await callback.answer("Экосистема не найдена", show_alert=True)
        return

    added = await _eb.add_member(
        pool, eco_id, callback.from_user.id, object_type, object_id
    )
    if added:
        await _eb.record_event(
            pool,
            eco_id,
            callback.from_user.id,
            "member_added",
            f"Добавлен {object_type} #{object_id} из фабрики",
            severity="info",
            details={"object_type": object_type, "object_id": object_id},
        )
        await callback.answer(f"✅ Добавлено в «{eco['name']}»", show_alert=True)
    else:
        await callback.answer(f"ℹ️ Уже в экосистеме «{eco['name']}»", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🌐 Открыть экосистему", callback_data=EcoCb(action="view", eco_id=eco_id)
    )
    kb.button(text="🌐 Все экосистемы", callback_data=EcoCb(action="menu"))
    kb.adjust(1)
    try:
        await callback.message.edit_text(
            f"✅ <b>{object_type.capitalize()} #{object_id}</b> добавлен в экосистему\n"
            f"<b>{html.escape(eco['name'])}</b>",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        pass

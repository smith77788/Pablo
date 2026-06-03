"""Intent Engine — Epoch IV. Управление целями, а не модулями."""

from __future__ import annotations

import json
import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    BmCb,
    GeoPresenceCb,
    StrikeCb,
    MassOpCb,
    HealthCb,
    EcoCb,
    IntentCb,
)
from bot.states import IntentFSM
from bot.utils.subscription import require_plan
from database import db
from services.intent_planner import (
    STRATEGY_LABELS,
    STRATEGY_DESCRIPTIONS,
    classify_intent,
    assess_resources,
    build_plan,
    forecast_execution,
    format_plan_card,
)
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router(name="intent_engine")


# ─── Preset intents ───────────────────────────────────────────────────────────

_PRESET_LABELS: dict[str, tuple[str, str]] = {
    "presence": ("🌍", "Создать присутствие"),
    "growth": ("📈", "Масштабировать сеть"),
    "sync": ("🔄", "Синхронизировать всё"),
    "audit": ("🔍", "Аудит инфраструктуры"),
    "network": ("🕸", "Новая сеть"),
    "strike": ("⚔️", "STRIKE"),
}

_NAVIGATE_LABELS: dict[str, str] = {
    "gp_factory": "🌍 Открыть GP Factory",
    "factory": "🏭 Открыть Factory",
    "mass_ops": "⚡ Открыть Mass Ops",
    "health_dashboard": "❤️ Открыть Здоровье",
    "ecosystems": "🌐 Открыть Экосистемы",
    "strike": "⚔️ Открыть STRIKE",
    "main": "🏠 Главное меню",
}


# ─── Keyboards ────────────────────────────────────────────────────────────────


def _intent_main_kb() -> object:
    kb = InlineKeyboardBuilder()
    for itype, (icon, label) in _PRESET_LABELS.items():
        kb.button(
            text=f"{icon} {label}", callback_data=IntentCb(action="preset", value=itype)
        )
    kb.button(text="✍️ Описать свою цель", callback_data=IntentCb(action="new"))
    kb.button(text="📋 История намерений", callback_data=IntentCb(action="history"))
    kb.button(text="◀️ Главное меню", callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 2, 1, 1, 1)
    return kb.as_markup()


def _plan_kb(intent_id: int, plan: dict, current_strategy: str) -> object:
    kb = InlineKeyboardBuilder()
    for s in ("safest", "balanced", "fastest", "scalable"):
        label = STRATEGY_LABELS[s]
        if s == current_strategy:
            label = f"✓ {label}"
        kb.button(
            text=label,
            callback_data=IntentCb(action="strategy", intent_id=intent_id, value=s),
        )
    kb.adjust(2, 2)

    if plan.get("executable") and plan.get("action") == "execute_gp":
        kb.button(
            text="🚀 Запустить план",
            callback_data=IntentCb(action="confirm", intent_id=intent_id),
        )
        kb.button(
            text="✏️ Настроить вручную",
            callback_data=IntentCb(action="manual", intent_id=intent_id),
        )
    elif plan.get("executable") and plan.get("action") == "run_audit":
        kb.button(
            text="🔍 Запустить аудит",
            callback_data=IntentCb(action="confirm", intent_id=intent_id),
        )
    else:
        nav_key = plan.get("navigate_to", "main")
        nav_label = _NAVIGATE_LABELS.get(nav_key, "▶️ Перейти к инструменту")
        kb.button(
            text=nav_label, callback_data=IntentCb(action="manual", intent_id=intent_id)
        )

    kb.button(
        text="❌ Отмена", callback_data=IntentCb(action="cancel", intent_id=intent_id)
    )
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()


def _history_kb(intents: list, page: int = 0) -> object:
    kb = InlineKeyboardBuilder()
    for row in intents:
        status_icons = {
            "draft": "📝",
            "ready": "✅",
            "executing": "⚙️",
            "completed": "✅",
            "failed": "❌",
            "cancelled": "⛔",
        }
        icon = status_icons.get(row["status"], "📋")
        itype_icons = {
            "presence": "🌍",
            "network": "🕸",
            "audit": "🔍",
            "sync": "🔄",
            "growth": "📈",
            "strike": "⚔️",
            "custom": "🎯",
        }
        type_icon = itype_icons.get(row["intent_type"], "🎯")
        label = f"{icon} {type_icon} {row['description'][:30]}"
        kb.button(
            text=label, callback_data=IntentCb(action="detail", intent_id=row["id"])
        )
    kb.button(text="🎯 Новое намерение", callback_data=IntentCb(action="menu"))
    kb.button(text="◀️ Назад", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


# ─── Main entry ───────────────────────────────────────────────────────────────


async def _show_intent_main(
    target: Message | CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
    edit: bool = False,
) -> None:
    await state.clear()
    owner_id = (target.from_user or target.message.from_user).id  # type: ignore[union-attr]

    resources = await assess_resources(pool, owner_id)
    acc_str = f"👤 {resources['accounts_available']} аккаунтов"
    prx_str = f"🌐 {resources['proxies_available']} прокси"
    ops_str = (
        f"⚙️ {resources['active_operations']} активных операций"
        if resources["active_operations"] > 0
        else ""
    )

    status_lines = [acc_str, prx_str]
    if ops_str:
        status_lines.append(ops_str)
    status_text = "  •  ".join(status_lines)

    text = (
        "🎯 <b>Intent Engine</b>\n"
        "<i>Эпоха IV — управляйте результатами, а не модулями</i>\n\n"
        "Сформулируйте цель — система построит план и запустит выполнение.\n\n"
        f"<b>Ресурсы:</b> {status_text}\n\n"
        "Выберите тип намерения или опишите свою цель:"
    )

    kb = _intent_main_kb()
    msg = target if isinstance(target, Message) else target.message
    if edit and isinstance(target, CallbackQuery):
        await msg.edit_text(text, reply_markup=kb)
    else:
        await msg.answer(text, reply_markup=kb)


@router.message(Command("intent"))
async def cmd_intent(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    await _show_intent_main(message, pool, state)


@router.callback_query(IntentCb.filter(F.action == "menu"))
async def cb_intent_menu(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await _show_intent_main(callback, pool, state, edit=True)


# ─── Custom description input ─────────────────────────────────────────────────


@router.callback_query(IntentCb.filter(F.action == "new"))
async def cb_intent_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(IntentFSM.describing)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "✍️ <b>Опишите вашу цель</b>\n\n"
        "Напишите что вы хотите сделать. Например:\n"
        "  • <i>Создать присутствие в европейских городах</i>\n"
        "  • <i>Масштабировать экосистему в 50 регионах</i>\n"
        "  • <i>Синхронизировать все каналы по шаблону</i>\n"
        "  • <i>Проверить здоровье инфраструктуры</i>\n\n"
        "Введите описание:",
        reply_markup=kb.as_markup(),
    )


@router.message(IntentFSM.describing)
async def fsm_intent_description(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    description = (message.text or "").strip()
    if not description:
        await message.answer("Введите описание цели.")
        return

    await state.clear()
    await _process_intent(message, pool, description)


# ─── Preset intent ────────────────────────────────────────────────────────────


@router.callback_query(IntentCb.filter(F.action == "preset"))
async def cb_intent_preset(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()

    preset_descriptions = {
        "presence": "Создать присутствие в европейских городах",
        "growth": "Масштабировать и усилить экосистему",
        "sync": "Синхронизировать всю инфраструктуру по шаблону",
        "audit": "Проверить здоровье всей инфраструктуры",
        "network": "Создать новую сеть каналов",
        "strike": "Запустить STRIKE против цели",
    }
    description = preset_descriptions.get(callback_data.value, callback_data.value)
    await _process_intent(callback.message, pool, description)


# ─── Core planning logic ──────────────────────────────────────────────────────


async def _process_intent(
    message: Message, pool: asyncpg.Pool, description: str
) -> None:
    owner_id = message.chat.id
    if not await require_plan(pool, owner_id, "starter"):
        await message.answer("🔒 Intent Engine доступен с планом Starter и выше.")
        return

    wait_msg = await message.answer("⏳ Анализирую цель и строю план…")

    try:
        intent_type = classify_intent(description)
        resources = await assess_resources(pool, owner_id)
        plan = await build_plan(pool, owner_id, intent_type, description, resources)
        forecast = forecast_execution(plan, strategy="balanced")

        intent_id = await db.create_intent(
            pool,
            owner_id,
            intent_type,
            description,
            plan,
            "balanced",
            forecast,
        )

        await _show_plan_card(
            wait_msg, pool, intent_id, plan, forecast, "balanced", edit=True
        )

    except Exception as e:
        log_exc_swallow(log, f"_process_intent failed: {e}")
        await wait_msg.edit_text(
            f"⚠️ Не удалось построить план: {type(e).__name__}\n"
            "Попробуйте ещё раз или выберите инструмент вручную."
        )


async def _show_plan_card(
    message: Message,
    pool: asyncpg.Pool,
    intent_id: int,
    plan: dict,
    forecast: dict,
    strategy: str,
    edit: bool = False,
) -> None:
    intent_type_labels = {
        "presence": "🌍 Создать присутствие",
        "network": "🕸 Создать сеть",
        "audit": "🔍 Аудит инфраструктуры",
        "sync": "🔄 Синхронизация",
        "growth": "📈 Масштабирование",
        "strike": "⚔️ STRIKE",
        "custom": "🎯 Пользовательское",
    }
    type_label = intent_type_labels.get(plan.get("intent_type", "custom"), "🎯")

    plan_text = format_plan_card(plan, forecast, strategy)
    strat_desc = STRATEGY_DESCRIPTIONS.get(strategy, "")

    text = (
        f"🎯 <b>Intent Engine — {type_label}</b>\n\n"
        f"{plan_text}\n\n"
        f"<i>Стратегия: {strat_desc}</i>"
    )

    kb = _plan_kb(intent_id, plan, strategy)
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


# ─── Strategy selection ───────────────────────────────────────────────────────


@router.callback_query(IntentCb.filter(F.action == "strategy"))
async def cb_intent_strategy(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
) -> None:
    intent_id = callback_data.intent_id
    new_strategy = callback_data.value

    if new_strategy not in ("safest", "balanced", "fastest", "scalable"):
        await callback.answer("Неверная стратегия")
        return

    row = await db.get_intent(pool, intent_id, callback.from_user.id)
    if not row:
        await callback.answer("Намерение не найдено")
        return

    plan = (
        json.loads(row["plan"]) if isinstance(row["plan"], str) else dict(row["plan"])
    )
    new_forecast = forecast_execution(plan, strategy=new_strategy)
    await db.update_intent_strategy(
        pool, intent_id, callback.from_user.id, new_strategy, new_forecast
    )

    await callback.answer(
        f"Стратегия: {STRATEGY_LABELS.get(new_strategy, new_strategy)}"
    )
    await _show_plan_card(
        callback.message, pool, intent_id, plan, new_forecast, new_strategy, edit=True
    )


# ─── Execute intent ───────────────────────────────────────────────────────────


@router.callback_query(IntentCb.filter(F.action == "confirm"))
async def cb_intent_confirm(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
) -> None:
    intent_id = callback_data.intent_id
    owner_id = callback.from_user.id

    row = await db.get_intent(pool, intent_id, owner_id)
    if not row:
        await callback.answer("Намерение не найдено")
        return

    plan = (
        json.loads(row["plan"]) if isinstance(row["plan"], str) else dict(row["plan"])
    )
    strategy = row["strategy"]
    action = plan.get("action", "navigate")

    await callback.answer("Запускаю…")

    if action == "execute_gp":
        await _execute_gp_intent(callback, pool, intent_id, plan, strategy, owner_id)
    elif action == "run_audit":
        await _execute_audit_intent(callback, pool, intent_id, owner_id)
    else:
        await _navigate_to_tool(callback, plan)


async def _execute_gp_intent(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    intent_id: int,
    plan: dict,
    strategy: str,
    owner_id: int,
) -> None:
    from services import operation_bus
    from services.geo_data import GEO_PRESETS
    from services.presence_planner import build_targets

    geo_preset = plan.get("geo_preset", "eu_capitals")
    asset_type = plan.get("asset_type", "channel")
    name_pattern = plan.get("name_pattern", "{{CITY_NAME}} News")
    username_pattern = plan.get("username_pattern", "news_{{CITY_SLUG}}")
    account_ids = plan.get("account_ids", [])

    if not account_ids:
        await callback.message.edit_text(
            "⚠️ Нет доступных аккаунтов для выполнения плана.\n"
            "Добавьте аккаунты через Активы → Аккаунты."
        )
        return

    preset_info = GEO_PRESETS.get(geo_preset)
    if not preset_info:
        await callback.message.edit_text("⚠️ Геопресет не найден.")
        return

    geo_list = preset_info["cities"]
    targets = build_targets(
        geo_list, asset_type, name_pattern, username_pattern, account_ids
    )

    _op_map = {
        "channel": "global_presence_channel",
        "group": "global_presence_group",
        "bot": "global_presence_bot",
        "package": "global_presence_package",
        "full_package": "global_presence_full_package",
    }
    op_type = _op_map.get(asset_type, "global_presence_channel")

    try:
        plan_id = await db.create_global_presence_plan(
            pool,
            owner_id=owner_id,
            asset_type=asset_type,
            name_pattern=name_pattern,
            username_pattern=username_pattern,
            geo_selection={
                "preset": geo_preset,
                "count": len(geo_list),
                "via_intent": intent_id,
            },
            account_selection={"account_ids": account_ids},
        )
        await db.create_global_presence_targets(pool, plan_id, targets)
        op_id = await operation_bus.submit(
            pool,
            owner_id,
            op_type,
            {"plan_id": plan_id},
            total_items=len(targets),
        )
        await db.link_plan_to_operation(pool, plan_id, op_id)
        await db.update_intent_status(pool, intent_id, owner_id, "executing")

        kb = InlineKeyboardBuilder()
        kb.button(
            text="📋 Отслеживать в очереди", callback_data=MassOpCb(action="queue")
        )
        kb.button(text="🎯 Intent Engine", callback_data=IntentCb(action="menu"))
        kb.adjust(1)

        asset_labels = {
            "channel": "каналов",
            "group": "групп",
            "bot": "ботов",
            "package": "пакетов",
            "full_package": "полных пакетов",
        }
        asset_label = asset_labels.get(asset_type, "объектов")
        await callback.message.edit_text(
            f"✅ <b>План запущен!</b>\n\n"
            f"🌍 Геопресет: {plan.get('geo_label', geo_preset)}\n"
            f"📦 Тип: {plan.get('asset_label', asset_type)}\n"
            f"📊 Объектов: {len(targets)} {asset_label}\n"
            f"🔢 Операция #{op_id}\n\n"
            f"Следите за прогрессом в очереди операций.",
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        log_exc_swallow(log, f"_execute_gp_intent: {e}")
        await callback.message.edit_text(
            f"⚠️ Ошибка при запуске: {type(e).__name__}: {e}"
        )


async def _execute_audit_intent(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    intent_id: int,
    owner_id: int,
) -> None:
    try:
        from services import infra_copilot

        insights = await infra_copilot.run_full_analysis(pool, owner_id)

        critical = [i for i in insights if getattr(i, "severity", "") == "critical"]
        warnings = [i for i in insights if getattr(i, "severity", "") == "warning"]
        infos = [i for i in insights if getattr(i, "severity", "") == "info"]

        lines = ["🔍 <b>Аудит инфраструктуры завершён</b>\n"]
        if critical:
            lines.append(f"🚨 <b>Критических проблем: {len(critical)}</b>")
            for i in critical[:3]:
                lines.append(f"  • {i.title}: {i.explanation[:100]}")
        if warnings:
            lines.append(f"⚠️ <b>Предупреждений: {len(warnings)}</b>")
            for i in warnings[:3]:
                lines.append(f"  • {i.title}: {i.explanation[:80]}")
        if infos:
            lines.append(f"ℹ️ Информационных: {len(infos)}")
        if not (critical or warnings or infos):
            lines.append("✅ <b>Инфраструктура в хорошем состоянии</b>")

        await db.update_intent_status(pool, intent_id, owner_id, "completed")
        await db.save_intent_feedback(
            pool,
            intent_id,
            owner_id,
            {
                "critical": len(critical),
                "warnings": len(warnings),
                "infos": len(infos),
            },
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="❤️ Детальный отчёт", callback_data=HealthCb(action="menu"))
        kb.button(text="🎯 Intent Engine", callback_data=IntentCb(action="menu"))
        kb.adjust(1)

        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )
    except Exception as e:
        log_exc_swallow(log, f"_execute_audit_intent: {e}")
        await callback.message.edit_text(f"⚠️ Ошибка аудита: {type(e).__name__}")


async def _navigate_to_tool(callback: CallbackQuery, plan: dict) -> None:
    nav_key = plan.get("navigate_to", "main")
    nav_map: dict[str, object] = {
        "gp_factory": GeoPresenceCb(action="menu"),
        "strike": StrikeCb(action="menu"),
        "mass_ops": MassOpCb(action="menu"),
        "health_dashboard": HealthCb(action="menu"),
        "ecosystems": EcoCb(action="menu"),
        "main": BmCb(action="main"),
        "factory": BmCb(action="operations"),
    }
    cb_data = nav_map.get(nav_key, BmCb(action="main"))

    kb = InlineKeyboardBuilder()
    nav_label = _NAVIGATE_LABELS.get(nav_key, "▶️ Перейти")
    kb.button(text=nav_label, callback_data=cb_data)
    kb.button(text="◀️ К планам", callback_data=IntentCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"📋 <b>План готов</b>\n\n"
        f"{plan.get('goal', '—')}\n\n"
        "Для выполнения перейдите к соответствующему инструменту:",
        reply_markup=kb.as_markup(),
    )


# ─── Manual navigation ────────────────────────────────────────────────────────


@router.callback_query(IntentCb.filter(F.action == "manual"))
async def cb_intent_manual(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
) -> None:
    intent_id = callback_data.intent_id
    owner_id = callback.from_user.id

    row = await db.get_intent(pool, intent_id, owner_id)
    if not row:
        await callback.answer("Намерение не найдено", show_alert=True)
        return

    plan = (
        json.loads(row["plan"]) if isinstance(row["plan"], str) else dict(row["plan"])
    )
    await callback.answer()
    await _navigate_to_tool(callback, plan)


# ─── Cancel ───────────────────────────────────────────────────────────────────


@router.callback_query(IntentCb.filter(F.action == "cancel"))
async def cb_intent_cancel(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer("Отменено")
    await state.clear()
    intent_id = callback_data.intent_id
    if intent_id:
        await db.update_intent_status(
            pool, intent_id, callback.from_user.id, "cancelled"
        )
    await _show_intent_main(callback, pool, state, edit=True)


# ─── History ──────────────────────────────────────────────────────────────────


@router.callback_query(IntentCb.filter(F.action == "history"))
async def cb_intent_history(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    owner_id = callback.from_user.id
    intents = await db.list_intents(pool, owner_id, limit=10)

    if not intents:
        await callback.message.edit_text(
            "📋 <b>История намерений</b>\n\nВы ещё не создавали намерений.\n"
            "Нажмите «Новое намерение» чтобы начать.",
            reply_markup=_history_kb([], 0),
        )
        return

    status_labels = {
        "draft": "📝 Черновик",
        "ready": "✅ Готов",
        "executing": "⚙️ Выполняется",
        "completed": "✅ Завершён",
        "failed": "❌ Ошибка",
        "cancelled": "⛔ Отменён",
    }
    lines = ["📋 <b>История намерений</b>\n"]
    for row in intents[:5]:
        st = status_labels.get(row["status"], row["status"])
        ts = row["created_at"].strftime("%d.%m %H:%M") if row.get("created_at") else ""
        lines.append(f"  {st} — {row['description'][:40]}  <i>{ts}</i>")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_history_kb(intents, 0),
    )


@router.callback_query(IntentCb.filter(F.action == "detail"))
async def cb_intent_detail(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
) -> None:
    await callback.answer()
    owner_id = callback.from_user.id
    row = await db.get_intent(pool, callback_data.intent_id, owner_id)
    if not row:
        await callback.answer("Не найдено", show_alert=True)
        return

    plan = (
        json.loads(row["plan"]) if isinstance(row["plan"], str) else dict(row["plan"])
    )
    forecast = (
        json.loads(row["forecast"])
        if isinstance(row["forecast"], str)
        else dict(row["forecast"])
    )
    strategy = row["strategy"]

    await _show_plan_card(
        callback.message, pool, row["id"], plan, forecast, strategy, edit=True
    )

"""Intent Engine: цель -> план -> безопасное действие или переход в инструмент."""

from __future__ import annotations

import html as _html
import json
import logging
from typing import Any

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import (
    AccCb,
    BmCb,
    EcoCb,
    GeoPresenceCb,
    HealthCb,
    IntentCb,
    MassOpCb,
    StrikeCb,
    VisCb,
)
from bot.states import IntentFSM
from bot.utils.subscription import require_plan
from database import db
from services import autonomous_engine
from services.intent_planner import (
    STRATEGY_DESCRIPTIONS,
    STRATEGY_LABELS,
    assess_resources,
    forecast_execution,
    format_plan_card,
)
from services.logger import log_exc_swallow

log = logging.getLogger(__name__)
router = Router(name="intent_engine")


_PRESET_LABELS: dict[str, tuple[str, str]] = {
    "presence": ("🌍", "Гео-присутствие"),
    "sync": ("🔁", "Синхронизация контента"),
    "audit": ("🩺", "Аудит инфраструктуры"),
    "network": ("🕸", "Карта сети"),
    "strike": ("⚔️", "STRIKE"),
    "visibility": ("🔎", "Видимость в поиске"),
}

_PRESET_DESCRIPTIONS: dict[str, str] = {
    "presence": "Создать гео-присутствие и распределить ресурсы по городам",
    "sync": "Синхронизировать контент между активами проекта",
    "audit": "Проверить здоровье аккаунтов, прокси, очередей и операций",
    "network": "Показать карту связей и состояние активов",
    "strike": "Открыть STRIKE для ручной проверки и легитимных жалоб",
    "visibility": "Проверить позиции, ключевые слова и видимость",
}

_NAVIGATE_LABELS: dict[str, str] = {
    "gp_factory": "🌍 Открыть GP Factory",
    "factory": "🏭 Открыть Factory",
    "mass_ops": "⚙️ Открыть Mass Ops",
    "health_dashboard": "🩺 Открыть здоровье",
    "ecosystems": "🧠 Открыть экосистемы",
    "strike": "⚔️ Открыть STRIKE",
    "main": "🏠 Открыть меню",
    "ranking": "🔎 Открыть Rankings",
}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    if isinstance(value, dict):
        return value
    return dict(value) if value is not None else {}


def _intent_main_kb() -> object:
    kb = InlineKeyboardBuilder()
    for intent_type, (icon, label) in _PRESET_LABELS.items():
        kb.button(
            text=f"{icon} {label}",
            callback_data=IntentCb(action="preset", value=intent_type),
        )
    kb.button(text="✍️ Своя цель", callback_data=IntentCb(action="new"))
    kb.button(text="📜 История", callback_data=IntentCb(action="history"))
    kb.button(text="🏠 Главное меню", callback_data=BmCb(action="main"))
    kb.adjust(2, 2, 2, 1, 2, 1)
    return kb.as_markup()


def _plan_kb(intent_id: int, plan: dict[str, Any], current_strategy: str) -> object:
    kb = InlineKeyboardBuilder()
    for strategy in ("safest", "balanced", "fastest", "scalable"):
        label = STRATEGY_LABELS.get(strategy, strategy)
        if strategy == current_strategy:
            label = f"✅ {label}"
        kb.button(
            text=label,
            callback_data=IntentCb(
                action="strategy", intent_id=intent_id, value=strategy
            ),
        )
    kb.adjust(2, 2)

    action = plan.get("action")
    if plan.get("executable") and action in {
        "execute_gp",
        "run_audit",
        "execute_sync",
        "run_visibility",
        "execute_op",
    }:
        kb.button(
            text="🚀 Запустить",
            callback_data=IntentCb(action="confirm", intent_id=intent_id),
        )
    else:
        nav_key = str(plan.get("navigate_to") or "main")
        kb.button(
            text=_NAVIGATE_LABELS.get(nav_key, "➡️ Открыть инструмент"),
            callback_data=IntentCb(action="manual", intent_id=intent_id),
        )

    kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
    kb.button(
        text="❌ Отмена", callback_data=IntentCb(action="cancel", intent_id=intent_id)
    )
    kb.adjust(2, 2, 1, 2)
    return kb.as_markup()


def _history_kb(intents: list[Any]) -> object:
    kb = InlineKeyboardBuilder()
    status_icons = {
        "draft": "📝",
        "ready": "✅",
        "executing": "⏳",
        "completed": "🏁",
        "failed": "⚠️",
        "cancelled": "❌",
    }
    type_icons = {
        "presence": "🌍",
        "network": "🕸",
        "audit": "🩺",
        "sync": "🔁",
        "strike": "⚔️",
        "visibility": "🔎",
        "custom": "✍️",
    }
    for row in intents:
        status = status_icons.get(row["status"], "•")
        icon = type_icons.get(row["intent_type"], "✍️")
        label = f"{status} {icon} {str(row['description'])[:30]}"
        kb.button(
            text=label, callback_data=IntentCb(action="detail", intent_id=row["id"])
        )
    kb.button(text="✍️ Новая цель", callback_data=IntentCb(action="new"))
    kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


async def _show_intent_main(
    target: Message | CallbackQuery,
    pool: asyncpg.Pool,
    state: FSMContext,
    edit: bool = False,
) -> None:
    await state.clear()
    owner_id = target.from_user.id
    try:
        resources = await assess_resources(pool, owner_id)
    except Exception as exc:
        log.warning("_show_intent_main: assess_resources failed: %s", exc)
        resources = {
            "accounts_available": 0,
            "accounts_avg_trust": 0.5,
            "proxies_available": 0,
            "active_operations": 0,
            "active_gp_plans": 0,
        }

    status_lines = [
        f"📱 Аккаунтов готово: <b>{resources['accounts_available']}</b>",
        f"🌐 Прокси доступно: <b>{resources['proxies_available']}</b>",
    ]
    active_ops = int(resources.get("active_operations") or 0)
    if active_ops:
        status_lines.append(f"⏳ Активных операций: <b>{active_ops}</b>")

    text = (
        "🎯 <b>Навигатор целей</b>\n"
        "<i>Опиши результат, а BotMother соберёт план и откроет нужный инструмент.</i>\n\n"
        + "\n".join(status_lines)
        + "\n\nВыбери готовый сценарий или напиши свою цель:"
    )

    if edit and isinstance(target, CallbackQuery) and target.message:
        await target.message.edit_text(text, reply_markup=_intent_main_kb(), parse_mode="HTML")
    elif isinstance(target, Message):
        await target.answer(text, reply_markup=_intent_main_kb(), parse_mode="HTML")


@router.message(Command("intent"))
async def cmd_intent(message: Message, pool: asyncpg.Pool, state: FSMContext) -> None:
    await _show_intent_main(message, pool, state)


@router.callback_query(IntentCb.filter(F.action == "menu"))
async def cb_intent_menu(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await _show_intent_main(callback, pool, state, edit=True)


@router.callback_query(IntentCb.filter(F.action == "new"))
async def cb_intent_new(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(IntentFSM.describing)
    kb = InlineKeyboardBuilder()
    kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    if callback.message:
        await callback.message.edit_text(
            "✍️ <b>Своя цель</b>\n\n"
            "Напиши, что нужно сделать. Примеры:\n"
            "• проверить здоровье инфраструктуры\n"
            "• создать гео-присутствие под выбранный проект\n"
            "• подготовить публикацию во все каналы\n"
            "• проверить видимость в поиске\n\n"
            "Жду описание:",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )


@router.message(IntentFSM.describing)
async def fsm_intent_description(
    message: Message, pool: asyncpg.Pool, state: FSMContext
) -> None:
    description = (message.text or "").strip()
    if not description:
        await message.answer("Опиши цель текстом.")
        return

    await state.clear()
    await _process_intent(message, pool, description, message.from_user.id)


@router.callback_query(IntentCb.filter(F.action == "preset"))
async def cb_intent_preset(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer()
    await state.clear()
    key = callback_data.value or ""
    description = _PRESET_DESCRIPTIONS.get(key, key)
    if callback.message:
        await _process_intent(
            callback.message, pool, description, callback.from_user.id
        )


async def _process_intent(
    message: Message, pool: asyncpg.Pool, description: str, owner_id: int
) -> None:
    if not await require_plan(pool, owner_id, "starter"):
        await message.answer("🔒 Навигатор целей доступен с тарифа Starter.")
        return

    wait_msg = await message.answer("🧠 Собираю план и проверяю ресурсы...")
    try:
        contract = await autonomous_engine.build_autonomous_contract(
            pool, owner_id, description
        )
        plan = contract.enriched_plan()
        forecast = contract.forecast
        strategy = contract.strategy
        intent_id = await db.create_intent(
            pool,
            owner_id,
            contract.intent_type,
            description,
            plan,
            strategy,
            forecast,
        )
        await _show_plan_card(wait_msg, intent_id, plan, forecast, strategy, edit=True)
    except Exception as exc:
        log_exc_swallow(log, f"_process_intent failed: {exc}")
        await wait_msg.edit_text(
            f"⚠️ Не удалось собрать план: {type(exc).__name__}\n"
            "Попробуй сформулировать цель короче или открой нужный раздел вручную через /menu."
        )


async def _show_plan_card(
    message: Message,
    intent_id: int,
    plan: dict[str, Any],
    forecast: dict[str, Any],
    strategy: str,
    edit: bool = False,
) -> None:
    type_labels = {
        "presence": "🌍 Гео-присутствие",
        "network": "🕸 Карта сети",
        "audit": "🩺 Аудит",
        "sync": "🔁 Синхронизация",
        "strike": "⚔️ STRIKE",
        "visibility": "🔎 Видимость",
        "custom": "✍️ Своя цель",
    }
    type_label = type_labels.get(str(plan.get("intent_type") or "custom"), "✍️ Цель")
    plan_text = format_plan_card(plan, forecast, strategy)
    autonomous_text = autonomous_engine.format_autonomous_block(
        plan, strategy=strategy, forecast=forecast
    )
    strategy_description = STRATEGY_DESCRIPTIONS.get(strategy, "")

    text = (
        f"🎯 <b>План: {type_label}</b>\n\n"
        f"{plan_text}\n\n"
        f"{autonomous_text}\n\n"
        f"<i>Стратегия: {strategy_description}</i>"
    )
    kb = _plan_kb(intent_id, plan, strategy)
    if edit:
        await message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(IntentCb.filter(F.action == "strategy"))
async def cb_intent_strategy(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
) -> None:
    intent_id = callback_data.intent_id
    new_strategy = callback_data.value or ""
    if new_strategy not in ("safest", "balanced", "fastest", "scalable"):
        await callback.answer("Неизвестная стратегия", show_alert=True)
        return

    row = await db.get_intent(pool, intent_id, callback.from_user.id)
    if not row:
        await callback.answer("План не найден", show_alert=True)
        return

    plan = _as_dict(row["plan"])
    forecast = forecast_execution(plan, strategy=new_strategy)
    autonomous = plan.get("autonomous")
    if isinstance(autonomous, dict):
        autonomous["strategy"] = new_strategy
    await db.update_intent_strategy(
        pool, intent_id, callback.from_user.id, new_strategy, forecast
    )
    await callback.answer(
        f"Стратегия: {STRATEGY_LABELS.get(new_strategy, new_strategy)}"
    )
    if callback.message:
        await _show_plan_card(
            callback.message, intent_id, plan, forecast, new_strategy, edit=True
        )


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
        await callback.answer("План не найден", show_alert=True)
        return

    plan = _as_dict(row["plan"])
    forecast = _as_dict(row["forecast"])
    strategy = row["strategy"]
    gate = autonomous_engine.execution_gate(plan, forecast)
    if not gate["go"]:
        blockers = gate.get("blockers") or ["План требует ручной проверки"]
        await callback.answer("Нужна ручная проверка", show_alert=True)
        if callback.message:
            await callback.message.edit_text(
                "🛑 <b>Autonomous Gate</b>\n\n"
                "План пока нельзя запускать автоматически:\n"
                + "\n".join(f"• {reason}" for reason in blockers[:5]),
                reply_markup=_plan_kb(intent_id, plan, strategy),
            )
        return

    await callback.answer("Запускаю")
    action = plan.get("action", "navigate")
    if action == "execute_gp":
        await _execute_gp_intent(callback, pool, intent_id, plan, owner_id)
    elif action == "run_audit":
        await _execute_audit_intent(callback, pool, intent_id, owner_id)
    elif action == "execute_sync":
        await _execute_sync_intent(callback, pool, intent_id, plan, owner_id)
    elif action == "run_visibility":
        await _execute_visibility_intent(callback, pool, intent_id, owner_id)
    elif action == "execute_op":
        await _execute_op_intent(callback, pool, intent_id, plan, owner_id)
    else:
        await _navigate_to_tool(callback, plan)


async def _execute_gp_intent(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    intent_id: int,
    plan: dict[str, Any],
    owner_id: int,
) -> None:
    from services import operation_bus
    from services.geo_data import GEO_PRESETS
    from services.presence_planner import build_targets

    geo_preset = str(plan.get("geo_preset") or "eu_capitals")
    asset_type = str(plan.get("asset_type") or "channel")
    account_ids = list(plan.get("account_ids") or [])
    if not account_ids:
        await _show_manual_hint(
            callback,
            "Нет выбранных аккаунтов. Открой аккаунты и выбери пул для запуска.",
            "accounts",
        )
        return

    preset_info = GEO_PRESETS.get(geo_preset)
    if not preset_info:
        await _show_manual_hint(callback, "Гео-пресет не найден.", "gp_factory")
        return

    targets = build_targets(
        preset_info["cities"],
        asset_type,
        str(plan.get("name_pattern") or "{{CITY_NAME}} News"),
        str(plan.get("username_pattern") or "news_{{CITY_SLUG}}"),
        account_ids,
    )
    op_type = {
        "channel": "global_presence_channel",
        "group": "global_presence_group",
        "bot": "global_presence_bot",
        "package": "global_presence_package",
        "full_package": "global_presence_full_package",
    }.get(asset_type, "global_presence_channel")

    try:
        plan_id = await db.create_global_presence_plan(
            pool,
            owner_id=owner_id,
            asset_type=asset_type,
            name_pattern=str(plan.get("name_pattern") or "{{CITY_NAME}} News"),
            username_pattern=str(plan.get("username_pattern") or "news_{{CITY_SLUG}}"),
            geo_selection={
                "preset": geo_preset,
                "count": len(preset_info["cities"]),
                "via_intent": intent_id,
            },
            account_selection={"account_ids": account_ids},
        )
        await db.create_global_presence_targets(pool, plan_id, targets)
        op_id = await operation_bus.submit(
            pool, owner_id, op_type, {"plan_id": plan_id}, total_items=len(targets)
        )
        await db.link_plan_to_operation(pool, plan_id, op_id)
        await db.update_intent_status(pool, intent_id, owner_id, "executing")
        await _show_operation_started(callback, op_id, f"Создано целей: {len(targets)}")
    except Exception as exc:
        log_exc_swallow(log, f"_execute_gp_intent: {exc}")
        await _show_manual_hint(
            callback,
            f"Не удалось запустить GP: {type(exc).__name__}",
            "gp_factory",
        )


async def _execute_audit_intent(
    callback: CallbackQuery, pool: asyncpg.Pool, intent_id: int, owner_id: int
) -> None:
    try:
        from services import infra_copilot

        insights = await infra_copilot.run_full_analysis(pool, owner_id)
        critical = [i for i in insights if getattr(i, "severity", "") == "critical"]
        warnings = [i for i in insights if getattr(i, "severity", "") == "warning"]
        infos = [i for i in insights if getattr(i, "severity", "") == "info"]
        lines = ["🩺 <b>Аудит инфраструктуры</b>\n"]
        if critical:
            lines.append(f"🚨 Критичных: <b>{len(critical)}</b>")
            for item in critical[:3]:
                lines.append(f"• {item.title}: {item.explanation[:100]}")
        if warnings:
            lines.append(f"⚠️ Предупреждений: <b>{len(warnings)}</b>")
            for item in warnings[:3]:
                lines.append(f"• {item.title}: {item.explanation[:80]}")
        if infos:
            lines.append(f"ℹ️ Наблюдений: <b>{len(infos)}</b>")
        if not (critical or warnings or infos):
            lines.append("✅ Критичных проблем не найдено.")

        await db.update_intent_status(pool, intent_id, owner_id, "completed")
        await db.save_intent_feedback(
            pool,
            intent_id,
            owner_id,
            {"critical": len(critical), "warnings": len(warnings), "infos": len(infos)},
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="🩺 Health Center", callback_data=HealthCb(action="menu"))
        kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
        kb.adjust(1)
        if callback.message:
            await callback.message.edit_text(
                "\n".join(lines), reply_markup=kb.as_markup()
            )
    except Exception as exc:
        log_exc_swallow(log, f"_execute_audit_intent: {exc}")
        await _show_manual_hint(
            callback, f"Аудит упал: {type(exc).__name__}", "health_dashboard"
        )


async def _execute_sync_intent(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    intent_id: int,
    plan: dict[str, Any],
    owner_id: int,
) -> None:
    from services import operation_bus

    try:
        rows = await pool.fetch(
            "SELECT DISTINCT channel_id FROM managed_channels WHERE owner_id=$1 LIMIT 50",
            owner_id,
        )
    except Exception as exc:
        log_exc_swallow(log, f"_execute_sync_intent: fetch channels failed: {exc}")
        rows = []

    channel_ids = [str(row["channel_id"]) for row in rows]
    if not channel_ids:
        # No channels — navigate to Mass Ops for manual setup
        await db.update_intent_status(pool, intent_id, owner_id, "ready")
        await _navigate_to_tool(callback, plan | {"navigate_to": "mass_ops"})
        return

    try:
        op_id = await operation_bus.submit(
            pool,
            owner_id,
            "bulk_bot_edit",
            {"targets": channel_ids, "action": "sync_template", "via_intent": intent_id},
            total_items=len(channel_ids),
        )
        await db.link_intent_operation(pool, intent_id, op_id)
        await db.update_intent_status(pool, intent_id, owner_id, "executing")
        await _show_operation_started(
            callback, op_id, f"Синхронизация {len(channel_ids)} каналов поставлена в очередь."
        )
    except Exception as exc:
        log_exc_swallow(log, f"_execute_sync_intent: submit failed: {exc}")
        await db.update_intent_status(pool, intent_id, owner_id, "ready")
        await _show_manual_hint(
            callback,
            f"Не удалось поставить синхронизацию в очередь: {type(exc).__name__}",
            "mass_ops",
        )


async def _execute_visibility_intent(
    callback: CallbackQuery, pool: asyncpg.Pool, intent_id: int, owner_id: int
) -> None:
    try:
        keywords_cnt = (
            await pool.fetchval(
                "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active=TRUE",
                owner_id,
            )
            or 0
        )
    except Exception as exc:
        log_exc_swallow(log, f"_execute_visibility_intent: fetch failed: {exc}")
        keywords_cnt = 0
    await db.update_intent_status(pool, intent_id, owner_id, "completed")
    await db.save_intent_feedback(
        pool, intent_id, owner_id, {"keywords_cnt": keywords_cnt}
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Rankings", callback_data=VisCb(action="dashboard"))
    kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    if callback.message:
        await callback.message.edit_text(
            f"🔎 <b>Видимость</b>\n\nАктивных ключевых слов: <b>{keywords_cnt}</b>",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )


async def _execute_op_intent(
    callback: CallbackQuery,
    pool: asyncpg.Pool,
    intent_id: int,
    plan: dict[str, Any],
    owner_id: int,
) -> None:
    """Execute a custom/autonomous intent via operation_bus."""
    from services import operation_bus

    autonomous = plan.get("autonomous") or {}
    resource_plan = autonomous.get("resource_plan") or {}
    op_type = resource_plan.get("op_type") or autonomous_engine._intent_to_op_type(
        str(plan.get("intent_type", "custom")), plan
    )
    account_ids: list[int] = resource_plan.get("primary_account_ids") or []
    n_targets = int(
        plan.get("n_targets") or plan.get("n_channels") or plan.get("n_bots") or 5
    )
    goal = str(plan.get("goal") or "")

    # Build params per op_type
    if op_type == "bulk_create_channels":
        params: dict = {
            "count": n_targets,
            "prefix": plan.get("name_pattern") or "Канал",
            "about": goal[:120],
            # pass first primary account; _exec_bulk_create_channels uses acc_id (single int)
            "acc_id": account_ids[0] if account_ids else 0,
        }
    elif op_type == "bulk_join":
        targets = plan.get("targets") or plan.get("channels") or []
        if not targets:
            await _show_manual_hint(
                callback,
                "Для массового вступления укажи список каналов в Mass Ops.",
                "mass_ops",
            )
            return
        params = {"targets": targets, "account_ids": account_ids}
    elif op_type == "bulk_leave":
        targets = plan.get("targets") or plan.get("channels") or []
        if not targets:
            await _show_manual_hint(
                callback,
                "Для массового выхода укажи список каналов в Mass Ops.",
                "mass_ops",
            )
            return
        # _exec_bulk_leave reads params["channels"], not "targets"
        params = {"channels": targets, "account_ids": account_ids}
    else:
        # Can't auto-submit (e.g. mass_publish needs message text) — redirect to tool
        await db.update_intent_status(pool, intent_id, owner_id, "ready")
        await _navigate_to_tool(callback, plan | {"navigate_to": "mass_ops"})
        return

    try:
        op_id = await operation_bus.submit(
            pool, owner_id, op_type, params, total_items=n_targets
        )
    except Exception as exc:
        log_exc_swallow(log, f"_execute_op_intent: submit failed: {exc}")
        if callback.message:
            await callback.message.edit_text(
                f"❌ Не удалось поставить операцию в очередь: {type(exc).__name__}\n"
                f"{str(exc)[:120]}",
                reply_markup=_plan_kb(intent_id, plan, "balanced"),
            )
        return

    await db.update_intent_status(pool, intent_id, owner_id, "executing")
    await db.save_intent_feedback(
        pool, intent_id, owner_id, {"op_id": op_id, "op_type": op_type}
    )
    detail = (
        f"Тип: <code>{op_type}</code>\n"
        f"Аккаунтов: <b>{len(account_ids)}</b>\n"
        f"Целей: <b>{n_targets}</b>\n\n"
        f"Отслеживай прогресс в /tasks"
    )
    await _show_operation_started(callback, op_id, detail)


async def _show_operation_started(
    callback: CallbackQuery, op_id: int, detail: str
) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Очередь операций", callback_data=MassOpCb(action="queue"))
    kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    if callback.message:
        await callback.message.edit_text(
            f"✅ <b>Операция #{op_id} запущена</b>\n\n{detail}",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )


async def _show_manual_hint(
    callback: CallbackQuery, text: str, nav_key: str = "main"
) -> None:
    kb = InlineKeyboardBuilder()
    kb.button(
        text=_NAVIGATE_LABELS.get(nav_key, "➡️ Открыть инструмент"),
        callback_data=_nav_callback(nav_key),
    )
    kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    if callback.message:
        await callback.message.edit_text(f"⚠️ {_html.escape(text)}", reply_markup=kb.as_markup(), parse_mode="HTML")


def _nav_callback(nav_key: str) -> object:
    nav_map: dict[str, object] = {
        "gp_factory": GeoPresenceCb(action="menu"),
        "accounts": AccCb(action="menu"),
        "strike": StrikeCb(action="menu"),
        "mass_ops": MassOpCb(action="menu"),
        "health_dashboard": HealthCb(action="menu"),
        "ecosystems": EcoCb(action="menu"),
        "main": BmCb(action="main"),
        "factory": BmCb(action="operations"),
        "ranking": VisCb(action="dashboard"),
    }
    return nav_map.get(nav_key, BmCb(action="main"))


async def _navigate_to_tool(callback: CallbackQuery, plan: dict[str, Any]) -> None:
    nav_key = str(plan.get("navigate_to") or "main")
    kb = InlineKeyboardBuilder()
    kb.button(
        text=_NAVIGATE_LABELS.get(nav_key, "➡️ Открыть инструмент"),
        callback_data=_nav_callback(nav_key),
    )
    kb.button(text="📍 Навигатор", callback_data=IntentCb(action="menu"))
    kb.adjust(1)
    if callback.message:
        await callback.message.edit_text(
            f"➡️ <b>Открываю нужный раздел</b>\n\n{_html.escape(plan.get('goal', 'Цель готова к ручному запуску.'))}",
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )


@router.callback_query(IntentCb.filter(F.action == "manual"))
async def cb_intent_manual(
    callback: CallbackQuery, callback_data: IntentCb, pool: asyncpg.Pool
) -> None:
    row = await db.get_intent(pool, callback_data.intent_id, callback.from_user.id)
    if not row:
        await callback.answer("План не найден", show_alert=True)
        return
    await callback.answer()
    await _navigate_to_tool(callback, _as_dict(row["plan"]))


@router.callback_query(IntentCb.filter(F.action == "cancel"))
async def cb_intent_cancel(
    callback: CallbackQuery,
    callback_data: IntentCb,
    pool: asyncpg.Pool,
    state: FSMContext,
) -> None:
    await callback.answer("Отменено")
    await state.clear()
    if callback_data.intent_id:
        await db.update_intent_status(
            pool, callback_data.intent_id, callback.from_user.id, "cancelled"
        )
    await _show_intent_main(callback, pool, state, edit=True)


@router.callback_query(IntentCb.filter(F.action == "history"))
async def cb_intent_history(
    callback: CallbackQuery, pool: asyncpg.Pool, state: FSMContext
) -> None:
    await callback.answer()
    await state.clear()
    intents = await db.list_intents(pool, callback.from_user.id, limit=10)
    if not intents:
        if callback.message:
            await callback.message.edit_text(
                "📜 <b>История целей</b>\n\nПока пусто. Создай первую цель.",
                reply_markup=_history_kb([]),
                parse_mode="HTML",
            )
        return

    lines = ["📜 <b>История целей</b>\n"]
    for row in intents[:5]:
        ts = row["created_at"].strftime("%d.%m %H:%M") if row.get("created_at") else ""
        lines.append(f"• {row['status']} — {str(row['description'])[:45]} <i>{ts}</i>")
    if callback.message:
        await callback.message.edit_text(
            "\n".join(lines), reply_markup=_history_kb(intents), parse_mode="HTML"
        )


@router.callback_query(IntentCb.filter(F.action == "detail"))
async def cb_intent_detail(
    callback: CallbackQuery, callback_data: IntentCb, pool: asyncpg.Pool
) -> None:
    row = await db.get_intent(pool, callback_data.intent_id, callback.from_user.id)
    if not row:
        await callback.answer("Не найдено", show_alert=True)
        return
    await callback.answer()
    if callback.message:
        await _show_plan_card(
            callback.message,
            row["id"],
            _as_dict(row["plan"]),
            _as_dict(row["forecast"]),
            row["strategy"],
            edit=True,
        )

"""Swarm routing management."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
import asyncpg
from bot.callbacks import SwarmCb
from bot.keyboards import swarm_menu, back_to_bot, subscription_locked_markup
from bot.utils.subscription import require_plan, locked_text
from database import db

router = Router()

ROLE_LABELS = {
    "entry": "🚪 Entry — точка входа трафика",
    "conversion": "💰 Conversion — конверсионный бот",
    "retention": "🔄 Retention — удержание пользователей",
    "general": "⚙️ General — универсальный",
}

@router.callback_query(SwarmCb.filter(F.action == "menu"))
async def cb_swarm_menu(callback: CallbackQuery, callback_data: SwarmCb,
                         pool: asyncpg.Pool) -> None:

    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("Swarm (умный роутинг трафика)", "enterprise"), parse_mode="HTML",
            reply_markup=subscription_locked_markup("enterprise"),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    swarm_status = "🟢 Активен в Swarm" if row.get("swarm_enabled") else "⚫ Не в Swarm"
    role = ROLE_LABELS.get(row.get("bot_role", "general"), "⚙️ General")
    cluster = row.get("cluster") or "default"
    mode = await db.get_system_mode(pool)
    await callback.message.edit_text(
        f"🧬 <b>Swarm — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "Swarm — это система умного распределения пользователей между вашими ботами. Когда новый человек приходит в одного бота, система может автоматически перенаправить его в другой бот, который сейчас лучше конвертирует.\n\n"
        "💡 <b>Как работает:</b>\n"
        "Каждый бот получает роль: Entry (точка входа), Conversion (продаёт), Retention (удерживает). Система сама решает, в какой бот направить пользователя, основываясь на статистике.\n\n"
        f"Статус: {swarm_status}\n"
        f"Роль: {role}\n"
        f"Кластер: <code>{cluster}</code>\n"
        f"🌐 Режим системы: <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=swarm_menu(callback_data.bot_id, row),
    )


@router.callback_query(SwarmCb.filter(F.action == "toggle"))
async def cb_swarm_toggle(callback: CallbackQuery, callback_data: SwarmCb,
                           pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    new_state = not row.get("swarm_enabled", False)
    await db.toggle_swarm(pool, callback_data.bot_id, new_state)
    row2 = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row2['username']}" if row2["username"] else row2["first_name"]
    swarm_status = "🟢 Активен в Swarm" if new_state else "⚫ Не в Swarm"
    role = ROLE_LABELS.get(row2.get("bot_role", "general"), "⚙️ General")
    mode = await db.get_system_mode(pool)
    await callback.message.edit_text(
        f"🧬 <b>Swarm — {label}</b>\n\n"
        f"Статус: {swarm_status}\n"
        f"Роль: {role}\n"
        f"Кластер: <code>{row2.get('cluster') or 'default'}</code>\n\n"
        f"🌐 Режим системы: <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=swarm_menu(callback_data.bot_id, row2),
    )
    await callback.answer("✅ Статус обновлён")


@router.callback_query(SwarmCb.filter(F.action == "stats"))
async def cb_swarm_stats(callback: CallbackQuery, callback_data: SwarmCb,
                          pool: asyncpg.Pool) -> None:

    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    await callback.answer()
    stats = await db.get_routing_stats(pool, callback_data.bot_id, days=7)
    metrics = await pool.fetchrow("SELECT * FROM bot_metrics WHERE bot_id=$1", callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    mode = await db.get_system_mode(pool)
    config = await db.get_mode_routing_config(mode)
    # Use 0 as default; also guard against DB returning NULL in numeric columns
    score = float(metrics["score"] or 0) if metrics else 0.0
    ctr   = float(metrics["ctr"] or 0) if metrics else 0.0
    conv  = float(metrics["conversion_rate"] or 0) if metrics else 0.0
    ret_d1 = float(metrics["retention_d1"] or 0) if metrics else 0.0

    await callback.message.edit_text(
        f"📊 <b>Routing Stats — {label}</b>\n\n"
        f"🌐 Режим: <b>{mode.upper()}</b>\n"
        f"Routing: {'✅ Включён' if config['routing_enabled'] else '❌ Отключён'}\n"
        f"Вероятность: {int(config['routing_probability']*100)}%\n\n"
        f"<b>За 7 дней:</b>\n"
        f"  Всего решений: {stats['total']}\n"
        f"  Перенаправлено: {stats['routed']}\n"
        f"  Оставлено: {stats['kept']}\n\n"
        f"<b>Метрики бота:</b>\n"
        f"  Score: {round(score, 3)}\n"
        f"  CTR: {round(ctr*100, 1)}%\n"
        f"  Conversion: {round(conv*100, 1)}%\n"
        f"  D1 Retention: {round(ret_d1*100, 1)}%",
        parse_mode="HTML",
        reply_markup=back_to_bot(callback_data.bot_id),
    )


@router.callback_query(SwarmCb.filter(F.action.startswith("role_")))
async def cb_swarm_role(callback: CallbackQuery, callback_data: SwarmCb,
                         pool: asyncpg.Pool) -> None:

    role = callback_data.action.replace("role_", "")
    if role not in ("entry", "conversion", "retention", "general"):
        await callback.answer("Неверная роль", show_alert=True)
        return
    await callback.answer()
    await db.set_bot_role(pool, callback_data.bot_id, role)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    label = f"@{row['username']}" if row and row["username"] else (row["first_name"] if row else "")
    swarm_status = "🟢 Активен в Swarm" if row and row.get("swarm_enabled") else "⚫ Не в Swarm"
    role_label = ROLE_LABELS.get(role, role)
    mode = await db.get_system_mode(pool)
    await callback.message.edit_text(
        f"🧬 <b>Swarm — {label}</b>\n\n"
        f"Статус: {swarm_status}\n"
        f"Роль: {role_label}\n"
        f"Кластер: <code>{row.get('cluster') or 'default'}</code>\n\n"
        f"🌐 Режим системы: <b>{mode.upper()}</b>",
        parse_mode="HTML",
        reply_markup=swarm_menu(callback_data.bot_id, row),
    )
    await callback.answer(f"✅ Роль: {role_label}")


MODE_DESCRIPTIONS = {
    "manual": "🟢 Полный ручной контроль",
    "assisted": "🟡 Система предлагает изменения",
    "autopilot": "🔵 Автоматическая оптимизация",
    "growth": "🔴 Агрессивная оптимизация конверсии",
    "experiment": "🟣 Максимальное тестирование",
    "stability": "⚫ Фиксированный роутинг",
}

@router.callback_query(SwarmCb.filter(F.action == "set_mode"))
async def cb_set_mode(callback: CallbackQuery, callback_data: SwarmCb,
                       pool: asyncpg.Pool) -> None:

    await callback.answer()
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import SwarmCb as SC
    kb = InlineKeyboardBuilder()
    for mode, desc in MODE_DESCRIPTIONS.items():
        kb.button(text=desc, callback_data=SC(action=f"mode_{mode}", bot_id=callback_data.bot_id))
    kb.button(text="◀️ Назад", callback_data=SC(action="menu", bot_id=callback_data.bot_id))
    kb.adjust(1)
    current = await db.get_system_mode(pool)
    await callback.message.edit_text(
        f"🌐 <b>Системный режим (текущий: {current.upper()})</b>\n\n"
        "Режим определяет поведение всего swarm:\n"
        "как и когда происходит роутинг, тестирование, оптимизация.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(SwarmCb.filter(F.action.startswith("mode_")))
async def cb_change_mode(callback: CallbackQuery, callback_data: SwarmCb,
                          pool: asyncpg.Pool) -> None:

    mode = callback_data.action.removeprefix("mode_")
    valid_modes = ["manual", "assisted", "autopilot", "growth", "experiment", "stability"]
    if mode not in valid_modes:
        await callback.answer("Неизвестный режим.", show_alert=True)
        return
    await callback.answer()
    await db.set_system_mode(pool, mode)
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Режим изменён.", show_alert=True)
        return
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    await callback.message.edit_text(
        f"🧬 <b>Swarm — {label}</b>\n\n"
        f"Статус: {'🟢 Включён' if row.get('swarm_enabled') else '⚫ Отключён'}\n"
        f"Роль: <b>{row.get('bot_role','general')}</b>\n"
        f"Кластер: <code>{row.get('cluster') or 'default'}</code>\n\n"
        f"🌐 Режим системы: <b>{mode.upper()}</b>\n"
        f"{MODE_DESCRIPTIONS.get(mode, '')}",
        parse_mode="HTML",
        reply_markup=swarm_menu(callback_data.bot_id, dict(row)),
    )
    await callback.answer(f"✅ Режим изменён: {mode.upper()}")

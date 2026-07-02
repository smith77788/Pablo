"""Stars Hub — Telegram Stars Yield Optimizer UI."""

from __future__ import annotations

import logging
from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg

from bot.callbacks import StarsCb
from bot.states import StarsExperimentFSM
from database import db
from services import stars_optimizer

log = logging.getLogger(__name__)

router = Router()


# ── Content types ─────────────────────────────────────────────────────────────

CONTENT_TYPES = {
    "message":      "💬 Сообщение",
    "media":        "📸 Медиа",
    "subscription": "🔔 Подписка",
    "gift":         "🎁 Подарок",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _back_btn() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0))
    return kb


def _main_kb() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Дашборд", callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0))
    kb.button(text="🧪 Эксперименты", callback_data=StarsCb(action="list_experiments", experiment_id=0, bot_id=0))
    kb.button(text="➕ Новый A/B тест", callback_data=StarsCb(action="create_start", experiment_id=0, bot_id=0))
    kb.button(text="💡 Рекомендации", callback_data=StarsCb(action="recommendations", experiment_id=0, bot_id=0))
    kb.adjust(1)
    return kb


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "menu"))
@router.callback_query(StarsCb.filter(F.action == "dashboard"))
async def cb_dashboard(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id

    try:
        # Revenue last 30 days
        revenue_row = await pool.fetchrow(
            """
            SELECT COALESCE(SUM(stars_amount), 0) AS total,
                   COALESCE(AVG(stars_amount), 0) AS avg_price
            FROM stars_transactions
            WHERE user_id = $1
              AND created_at >= NOW() - INTERVAL '30 days'
            """,
            uid,
        )
        total_stars = int(revenue_row["total"]) if revenue_row else 0
        avg_price = round(float(revenue_row["avg_price"]), 1) if revenue_row else 0.0

        # Top-3 content by conversion rate (from completed experiments)
        top_rows = await pool.fetch(
            """
            SELECT name,
                   CASE WHEN winner = 'a' THEN conversions_a::float / NULLIF(impressions_a, 0)
                        WHEN winner = 'b' THEN conversions_b::float / NULLIF(impressions_b, 0)
                        ELSE GREATEST(
                            conversions_a::float / NULLIF(impressions_a, 0),
                            conversions_b::float / NULLIF(impressions_b, 0)
                        )
                   END AS best_cr
            FROM stars_experiments
            WHERE owner_id = $1
            ORDER BY best_cr DESC NULLS LAST
            LIMIT 3
            """,
            uid,
        )

        top_text = ""
        for i, r in enumerate(top_rows, 1):
            cr = round((r["best_cr"] or 0) * 100, 1)
            top_text += f"\n  {i}. <b>{r['name']}</b> — CR {cr}%"

        # Active experiments count
        active_count = await pool.fetchval(
            "SELECT COUNT(*) FROM stars_experiments WHERE owner_id = $1 AND status = 'active'",
            uid,
        )

        text = (
            "⭐ <b>Stars Yield Optimizer</b>\n\n"
            f"📅 Доход за 30 дней: <b>{total_stars} Stars</b>\n"
            f"📊 Средняя цена: <b>{avg_price} Stars</b>\n"
            f"🧪 Активных экспериментов: <b>{active_count}</b>\n"
        )
        if top_text:
            text += f"\n🏆 Топ контент по конверсии:{top_text}\n"

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_main_kb().as_markup())
    except Exception as e:
        log.error("stars_hub cb_dashboard: %s", e)
        from bot.callbacks import BmCb
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="growth"))
        await callback.message.edit_text(
            "⭐ <b>Stars Yield Optimizer</b>\n\n"
            "⚠️ Модуль недоступен — таблицы не созданы в базе данных.\n\n"
            "Администратору необходимо применить миграцию <code>schema_v118.sql</code>.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )


# ── List experiments ──────────────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "list_experiments"))
async def cb_list_experiments(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id

    try:
        rows = await pool.fetch(
            """
            SELECT id, name, status, winner, price_a, price_b,
                   impressions_a, impressions_b, conversions_a, conversions_b
            FROM stars_experiments
            WHERE owner_id = $1
            ORDER BY created_at DESC
            LIMIT 30
            """,
            uid,
        )
    except Exception as e:
        log.error("stars_hub cb_list_experiments: %s", e)
        await callback.message.edit_text(
            "🧪 <b>Эксперименты</b>\n\n"
            "⚠️ Таблицы не созданы. Применить <code>schema_v118.sql</code>.",
            parse_mode="HTML",
            reply_markup=_back_btn().as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    if not rows:
        text = "🧪 <b>Эксперименты</b>\n\nУ вас ещё нет A/B тестов. Создайте первый!"
    else:
        text = "🧪 <b>Ваши A/B эксперименты</b>\n\nВыберите для просмотра деталей:"
        for r in rows:
            status_icon = {"active": "🟢", "paused": "⏸", "completed": "✅"}.get(r["status"], "❓")
            winner_str = f" [{'A' if r['winner'] == 'a' else 'B'} победил]" if r["winner"] else ""
            label = f"{status_icon} {r['name']} ({r['price_a']}★ vs {r['price_b']}★){winner_str}"
            kb.button(
                text=label,
                callback_data=StarsCb(action="detail", experiment_id=r["id"], bot_id=0),
            )
    kb.button(text="➕ Новый тест", callback_data=StarsCb(action="create_start", experiment_id=0, bot_id=0))
    kb.button(text="◀️ Главное", callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0))
    kb.adjust(1)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Experiment detail ─────────────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "detail"))
async def cb_detail(callback: CallbackQuery, callback_data: StarsCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    exp_id = callback_data.experiment_id

    row = await pool.fetchrow("SELECT * FROM stars_experiments WHERE id = $1", exp_id)
    if not row:
        await callback.answer("Эксперимент не найден", show_alert=True)
        return

    cr_a = round(row["conversions_a"] / row["impressions_a"] * 100, 1) if row["impressions_a"] else 0.0
    cr_b = round(row["conversions_b"] / row["impressions_b"] * 100, 1) if row["impressions_b"] else 0.0

    status_label = {"active": "🟢 Активен", "paused": "⏸ Приостановлен", "completed": "✅ Завершён"}.get(
        row["status"], row["status"]
    )
    winner_str = ""
    if row["winner"]:
        wl = "A" if row["winner"] == "a" else "B"
        winner_str = f"\n🏆 Победитель: вариант <b>{wl}</b>"

    sig_str = ""
    if row["significance"] is not None:
        sig_str = f"\n📐 p-value: <b>{round(row['significance'], 4)}</b>"

    text = (
        f"🧪 <b>{row['name']}</b>\n\n"
        f"Тип контента: {CONTENT_TYPES.get(row['content_type'], row['content_type'])}\n"
        f"Статус: {status_label}\n"
        f"\n<b>Вариант A</b> — {row['price_a']} Stars\n"
        f"  Показов: {row['impressions_a']} | Покупок: {row['conversions_a']}\n"
        f"  CR: <b>{cr_a}%</b> | Выручка: {row['revenue_a']} Stars\n"
        f"\n<b>Вариант B</b> — {row['price_b']} Stars\n"
        f"  Показов: {row['impressions_b']} | Покупок: {row['conversions_b']}\n"
        f"  CR: <b>{cr_b}%</b> | Выручка: {row['revenue_b']} Stars"
        f"{sig_str}{winner_str}"
    )

    kb = InlineKeyboardBuilder()
    if row["status"] == "active":
        kb.button(
            text="📊 Пересчитать",
            callback_data=StarsCb(action="evaluate", experiment_id=exp_id, bot_id=0),
        )
        kb.button(
            text="⏸ Приостановить",
            callback_data=StarsCb(action="pause", experiment_id=exp_id, bot_id=0),
        )
    elif row["status"] == "paused":
        kb.button(
            text="▶️ Возобновить",
            callback_data=StarsCb(action="resume", experiment_id=exp_id, bot_id=0),
        )
    kb.button(
        text="🗑 Удалить",
        callback_data=StarsCb(action="delete_confirm", experiment_id=exp_id, bot_id=0),
    )
    kb.button(
        text="◀️ Список",
        callback_data=StarsCb(action="list_experiments", experiment_id=0, bot_id=0),
    )
    kb.adjust(1)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Evaluate (manual re-check) ────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "evaluate"))
async def cb_evaluate(callback: CallbackQuery, callback_data: StarsCb, pool: asyncpg.Pool) -> None:
    # Answer immediately to avoid Telegram timeout; show result via alert
    result = await stars_optimizer.evaluate_experiment(pool, callback_data.experiment_id)
    p = round(result.get("p_value", 1.0), 4)
    cr_a = round(result.get("cr_a", 0) * 100, 1)
    cr_b = round(result.get("cr_b", 0) * 100, 1)
    winner = result.get("winner")
    if winner:
        wl = "A" if winner == "a" else "B"
        msg = f"✅ Победитель — вариант {wl}! CR-A: {cr_a}%, CR-B: {cr_b}%, p={p}"
    else:
        msg = f"📊 Данных пока недостаточно. CR-A: {cr_a}%, CR-B: {cr_b}%, p={p}"
    await callback.answer(msg, show_alert=True)
    # refresh detail view
    from types import SimpleNamespace
    await cb_detail(callback, SimpleNamespace(experiment_id=callback_data.experiment_id, bot_id=0), pool)


# ── Pause / Resume ────────────────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "pause"))
async def cb_pause(callback: CallbackQuery, callback_data: StarsCb, pool: asyncpg.Pool) -> None:
    await pool.execute(
        "UPDATE stars_experiments SET status = 'paused' WHERE id = $1",
        callback_data.experiment_id,
    )
    await callback.answer("Эксперимент приостановлен", show_alert=False)
    from types import SimpleNamespace
    await cb_detail(callback, SimpleNamespace(experiment_id=callback_data.experiment_id, bot_id=0), pool)


@router.callback_query(StarsCb.filter(F.action == "resume"))
async def cb_resume(callback: CallbackQuery, callback_data: StarsCb, pool: asyncpg.Pool) -> None:
    await pool.execute(
        "UPDATE stars_experiments SET status = 'active' WHERE id = $1",
        callback_data.experiment_id,
    )
    await callback.answer("Эксперимент возобновлён", show_alert=False)
    from types import SimpleNamespace
    await cb_detail(callback, SimpleNamespace(experiment_id=callback_data.experiment_id, bot_id=0), pool)


# ── Delete ────────────────────────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "delete_confirm"))
async def cb_delete_confirm(callback: CallbackQuery, callback_data: StarsCb, pool: asyncpg.Pool) -> None:
    await callback.answer()
    row = await pool.fetchrow("SELECT name FROM stars_experiments WHERE id = $1", callback_data.experiment_id)
    name = row["name"] if row else "?"
    kb = InlineKeyboardBuilder()
    kb.button(
        text="🗑 Да, удалить",
        callback_data=StarsCb(action="delete_do", experiment_id=callback_data.experiment_id, bot_id=0),
    )
    kb.button(
        text="◀️ Отмена",
        callback_data=StarsCb(action="detail", experiment_id=callback_data.experiment_id, bot_id=0),
    )
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚠️ Удалить эксперимент <b>{name}</b>?\nВсе данные будут потеряны.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(StarsCb.filter(F.action == "delete_do"))
async def cb_delete_do(callback: CallbackQuery, callback_data: StarsCb, pool: asyncpg.Pool) -> None:
    await pool.execute("DELETE FROM stars_experiments WHERE id = $1", callback_data.experiment_id)
    await callback.answer("Удалено", show_alert=False)
    # go back to list
    from types import SimpleNamespace
    await cb_list_experiments(callback, pool)


# ── Recommendations ───────────────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "recommendations"))
async def cb_recommendations(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id

    # Get the first bot of the user for context (recommendations are per-owner).
    # managed_bots использует added_by (не owner_id); stars_experiments.bot_id хранит
    # telegram bot_id, поэтому выбираем bot_id, а не внутренний serial id.
    bot_row = await pool.fetchrow("SELECT bot_id FROM managed_bots WHERE added_by = $1 LIMIT 1", uid)
    bot_id = bot_row["bot_id"] if bot_row else 0

    recs = await stars_optimizer.get_recommendations(pool, bot_id, uid)
    text = "💡 <b>Рекомендации по ценообразованию</b>\n\n" + "\n\n".join(recs)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Главное", callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Create experiment FSM ─────────────────────────────────────────────────────

@router.callback_query(StarsCb.filter(F.action == "create_start"))
async def cb_create_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    uid = callback.from_user.id
    bots = await db.get_bots(pool, uid)

    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0))
        await callback.message.edit_text(
            "⚠️ У вас нет добавленных ботов. Сначала добавьте бота.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    kb = InlineKeyboardBuilder()
    for b in bots:
        label = f"@{b['username']}" if b.get("username") else b.get("first_name", "Bot")
        kb.button(
            text=f"🤖 {label}",
            callback_data=StarsCb(action="create_pick_bot", experiment_id=0, bot_id=b["id"]),
        )
    kb.button(text="◀️ Отмена", callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0))
    kb.adjust(1)
    await state.set_state(StarsExperimentFSM.choosing_bot)
    await callback.message.edit_text(
        "🧪 <b>Новый A/B эксперимент</b>\n\nШаг 1/5 — Выберите бота:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(StarsCb.filter(F.action == "create_pick_bot"))
async def cb_create_pick_bot(callback: CallbackQuery, callback_data: StarsCb, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(bot_id=callback_data.bot_id)
    await state.set_state(StarsExperimentFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Отмена", callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0))
    await callback.message.edit_text(
        "🧪 <b>Новый A/B эксперимент</b>\n\nШаг 2/5 — Введите название эксперимента:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(StarsExperimentFSM.waiting_name)
async def fsm_waiting_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip() if message.text else ""
    if not name or len(name) > 128:
        await message.answer("⚠️ Название должно быть от 1 до 128 символов. Попробуйте снова.")
        return
    await state.update_data(name=name)
    await state.set_state(StarsExperimentFSM.waiting_ctype)

    kb = InlineKeyboardBuilder()
    for ctype, label in CONTENT_TYPES.items():
        kb.button(
            text=label,
            callback_data=StarsCb(
                action=f"create_ctype_{ctype}",
                experiment_id=0,
                bot_id=0,
            ),
        )
    kb.adjust(2)
    await message.answer(
        "🧪 Шаг 3/5 — Выберите тип контента:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data.startswith("strs:create_ctype_"), StateFilter(StarsExperimentFSM.waiting_ctype))
async def cb_create_ctype(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    # Extract ctype from callback data string
    raw = callback.data  # e.g. "strs:create_ctype_message:0:0"
    parts = raw.split(":")
    action_part = parts[1]  # "create_ctype_message"
    ctype = action_part.replace("create_ctype_", "")

    if ctype not in CONTENT_TYPES:
        await callback.answer("Неверный тип", show_alert=True)
        return
    await state.update_data(content_type=ctype)
    await state.set_state(StarsExperimentFSM.waiting_price_a)
    await callback.message.edit_text(
        f"🧪 Шаг 4/5 — Введите цену <b>варианта A</b> в Stars (число):",
        parse_mode="HTML",
    )


@router.message(StarsExperimentFSM.waiting_price_a)
async def fsm_waiting_price_a(message: Message, state: FSMContext) -> None:
    text = message.text.strip() if message.text else ""
    if not text.isdigit() or int(text) < 1:
        await message.answer("⚠️ Введите целое положительное число Stars.")
        return
    await state.update_data(price_a=int(text))
    await state.set_state(StarsExperimentFSM.waiting_price_b)
    await message.answer(
        "🧪 Шаг 5/5 — Введите цену <b>варианта B</b> в Stars (число):",
        parse_mode="HTML",
    )


@router.message(StarsExperimentFSM.waiting_price_b)
async def fsm_waiting_price_b(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    text = message.text.strip() if message.text else ""
    if not text.isdigit() or int(text) < 1:
        await message.answer("⚠️ Введите целое положительное число Stars.")
        return
    data = await state.get_data()
    price_b = int(text)
    price_a = data["price_a"]
    if price_a == price_b:
        await message.answer("⚠️ Цены A и B должны быть разными. Введите другую цену.")
        return

    exp = await stars_optimizer.create_experiment(
        pool=pool,
        bot_id=data["bot_id"],
        owner_id=message.from_user.id,
        name=data["name"],
        content_type=data["content_type"],
        price_a=price_a,
        price_b=price_b,
    )
    await state.clear()

    kb = InlineKeyboardBuilder()
    kb.button(
        text="🔍 Детали",
        callback_data=StarsCb(action="detail", experiment_id=exp.id, bot_id=0),
    )
    kb.button(
        text="◀️ Главное",
        callback_data=StarsCb(action="dashboard", experiment_id=0, bot_id=0),
    )
    kb.adjust(1)
    await message.answer(
        f"✅ <b>Эксперимент создан!</b>\n\n"
        f"Название: <b>{exp.name}</b>\n"
        f"Вариант A: <b>{exp.price_a} Stars</b>\n"
        f"Вариант B: <b>{exp.price_b} Stars</b>\n\n"
        f"Используйте API record_impression / record_conversion для сбора данных.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

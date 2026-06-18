"""A/B experiment management."""

import math
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncpg
from bot.callbacks import ExperimentCb, BmCb
from bot.keyboards import (
    experiments_menu,
    experiment_view_menu,
    experiment_type_menu,
    variant_pick_menu,
    subscription_locked_markup,
)
from bot.utils.subscription import require_plan, locked_text
from database import db

router = Router()


class CreateExperiment(StatesGroup):
    waiting_name = State()
    waiting_variant_name = State()
    waiting_variant_content = State()


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _z_test_two_proportions(
    n_a: int, c_a: int, n_b: int, c_b: int
) -> tuple[float, str]:
    """
    Z-test for two proportions. Returns (z_score, confidence_label).
    Uses pooled proportion. Requires n >= 5 and at least some conversions.
    """
    if n_a < 5 or n_b < 5 or (c_a + c_b) == 0:
        return 0.0, "⚪ Мало данных"
    p_a = c_a / n_a
    p_b = c_b / n_b
    p_pool = (c_a + c_b) / (n_a + n_b)
    denom = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if denom == 0:
        return 0.0, "⚪ Нет вариации"
    z = abs(p_a - p_b) / denom
    if z >= 2.576:
        label = "🟢 Значимо (99%)"
    elif z >= 1.96:
        label = "🟡 Значимо (95%)"
    elif z >= 1.645:
        label = "🟠 Слабая знач. (90%)"
    else:
        label = "⚪ Недостаточно данных"
    return z, label


async def _owns_experiment(pool: asyncpg.Pool, exp_id: int, user_id: int) -> bool:
    return bool(
        await pool.fetchval(
            """SELECT 1 FROM experiments e
           JOIN managed_bots b ON b.bot_id = e.bot_id
           WHERE e.id=$1 AND b.added_by=$2""",
            exp_id,
            user_id,
        )
    )


async def _exp_text(exp, variants: list) -> str:
    status_label = {
        "draft": "📝 Черновик",
        "active": "🟢 Активен",
        "paused": "⏸ Пауза",
        "completed": "✅ Завершён",
    }
    type_label = {
        "start_message": "/start сообщение",
        "auto_reply": "Авто-ответ",
        "funnel": "Воронка",
    }
    safe_name = _html_escape(exp["name"])
    lines = [
        f"🧪 <b>Эксперимент: {safe_name}</b>",
        f"Тип: {type_label.get(exp['experiment_type'], _html_escape(exp['experiment_type']))}",
        f"Статус: {status_label.get(exp['status'], _html_escape(exp['status']))}",
        f"Вариантов: {len(variants)}",
        "",
        "<b>Варианты:</b>",
    ]
    ctrs = []
    for v in variants:
        ctr = (
            round(v["conversions"] / v["impressions"] * 100, 1)
            if v["impressions"]
            else 0
        )
        ctrs.append((v, ctr))

    # Find best variant by CTR
    if ctrs:
        best_ctr = max(c for _, c in ctrs)
    else:
        best_ctr = 0

    for v, ctr in ctrs:
        winner_mark = " 🏆" if exp.get("winner_variant_id") == v["id"] else ""
        best_mark = " ⭐" if ctr == best_ctr and ctr > 0 and not winner_mark else ""
        safe_vname = _html_escape(v["name"])
        lines.append(
            f"  • <b>{safe_vname}{winner_mark}{best_mark}</b>: "
            f"{v['impressions']} показов, {v['conversions']} конв. ({ctr}%)"
        )
        lines.append(f"    {_html_escape((v.get('content') or '')[:80])}…")

    # Statistical significance (only for exactly 2 variants with data)
    if len(variants) == 2:
        v0, v1 = variants[0], variants[1]
        n0, c0 = int(v0["impressions"] or 0), int(v0["conversions"] or 0)
        n1, c1 = int(v1["impressions"] or 0), int(v1["conversions"] or 0)
        _, sig_label = _z_test_two_proportions(n0, c0, n1, c1)
        lines.append(f"\n<b>Статистическая значимость:</b> {sig_label}")
        if n0 + n1 > 0:
            total = n0 + n1
            lines.append(
                f"Всего показов: {total} | "
                f"Нужно для 95% уверенности: ~{max(0, 200 - total)} ещё"
            )

    return "\n".join(lines)


@router.callback_query(ExperimentCb.filter(F.action == "list"))
async def cb_exp_list(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.message.edit_text(
            locked_text("A/B тесты", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup(
                "pro", back_callback=BmCb(action="main")
            ),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Главное меню", callback_data=BmCb(action="main"))
        kb.adjust(1)
        await callback.message.edit_text(
            "❌ Бот не найден.", parse_mode="HTML", reply_markup=kb.as_markup()
        )
        return
    exps = await db.get_experiments(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    active = sum(1 for e in exps if e["status"] == "active")
    if not exps:
        empty_hint = (
            "\n\n💡 <b>Начните первый эксперимент!</b>\n"
            "Нажмите «➕ Новый эксперимент» — создайте 2 варианта текста и запустите тест. "
            "Через несколько дней выберите победителя по статистике."
        )
    else:
        empty_hint = ""
    await callback.message.edit_text(
        f"🧪 <b>A/B Тесты — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "A/B тест — это когда вы показываете разным пользователям разные версии сообщения и смотрите, какая лучше работает. Например: одним пришёл заголовок «Привет!», другим «Добро пожаловать!» — и вы видите, после какого больше людей остаётся.\n\n"
        "💡 <b>Как использовать:</b>\n"
        "Создайте эксперимент → добавьте 2+ варианта → запустите → через несколько дней выберите победителя.\n\n"
        f"Экспериментов: <b>{len(exps)}</b> | Активных: <b>{active}</b>"
        f"{empty_hint}",
        parse_mode="HTML",
        reply_markup=experiments_menu(callback_data.bot_id, exps),
    )


@router.callback_query(ExperimentCb.filter(F.action == "view"))
async def cb_exp_view(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    if not await _owns_experiment(pool, callback_data.exp_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.answer()
    exp = await db.get_experiment(pool, callback_data.exp_id)
    if not exp:
        await callback.message.edit_text("❌ Эксперимент не найден.", parse_mode="HTML")
        return
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=experiment_view_menu(
            callback_data.bot_id, callback_data.exp_id, exp["status"]
        ),
    )


@router.callback_query(ExperimentCb.filter(F.action == "create"))
async def cb_exp_create(
    callback: CallbackQuery,
    callback_data: ExperimentCb,
    state: FSMContext,
    pool: asyncpg.Pool,
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("A/B тесты", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro", back_callback=BmCb(action="analytics")),
        )
        return
    await callback.answer()
    await state.set_state(CreateExperiment.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🧪 <b>Новый эксперимент</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=experiment_type_menu(callback_data.bot_id),
    )


@router.callback_query(ExperimentCb.filter(F.action.in_({"type_start", "type_reply"})))
async def cb_exp_type(
    callback: CallbackQuery, callback_data: ExperimentCb, state: FSMContext
) -> None:
    await callback.answer()
    exp_type = "start_message" if callback_data.action == "type_start" else "auto_reply"
    await state.update_data(exp_type=exp_type)
    await state.set_state(CreateExperiment.waiting_name)
    bot_id = callback_data.bot_id
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(
        text="❌ Отмена", callback_data=ExperimentCb(action="list", bot_id=bot_id)
    )
    await callback.message.edit_text(
        "🧪 <b>Новый эксперимент</b>\n\nВведите название эксперимента:",
        parse_mode="HTML",
        reply_markup=cancel_kb.as_markup(),
    )


@router.message(CreateExperiment.waiting_name, F.text)
async def msg_exp_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    name = message.text.strip()
    exp_id = await db.create_experiment(
        pool, data["bot_id"], name, data.get("exp_type", "start_message")
    )
    await state.update_data(exp_id=exp_id)
    await state.set_state(CreateExperiment.waiting_variant_name)
    safe_name = _html_escape(name)
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(
        text="❌ Отмена",
        callback_data=ExperimentCb(action="list", bot_id=data["bot_id"]),
    )
    await message.answer(
        f"✅ Эксперимент «{safe_name}» создан!\n\n"
        "Добавьте первый вариант.\n"
        "Введите название варианта (например: «Контроль» или «Вариант A»):",
        parse_mode="HTML",
        reply_markup=cancel_kb.as_markup(),
    )


@router.message(CreateExperiment.waiting_variant_name, F.text)
async def msg_variant_name(message: Message, state: FSMContext) -> None:
    variant_name = message.text.strip()
    data = await state.get_data()
    await state.update_data(variant_name=variant_name)
    await state.set_state(CreateExperiment.waiting_variant_content)
    safe_vname = _html_escape(variant_name)
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(
        text="❌ Отмена",
        callback_data=ExperimentCb(action="list", bot_id=data.get("bot_id", 0)),
    )
    await message.answer(
        f"Вариант: <b>{safe_vname}</b>\n\n"
        "Введите содержимое (текст /start сообщения или авто-ответа):",
        parse_mode="HTML",
        reply_markup=cancel_kb.as_markup(),
    )


@router.message(CreateExperiment.waiting_variant_content, F.text)
async def msg_variant_content(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await db.add_experiment_variant(
        pool, data["exp_id"], data["variant_name"], message.text
    )
    variants = await db.get_experiment_variants(pool, data["exp_id"])
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import ExperimentCb as EC

    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Ещё вариант",
        callback_data=EC(
            action="add_variant", bot_id=data["bot_id"], exp_id=data["exp_id"]
        ),
    )
    kb.button(
        text="▶️ Запустить эксперимент",
        callback_data=EC(action="start", bot_id=data["bot_id"], exp_id=data["exp_id"]),
    )
    kb.adjust(1)
    await state.clear()
    safe_vname = _html_escape(data["variant_name"])
    await message.answer(
        f"✅ Вариант «{safe_vname}» добавлен!\n"
        f"Всего вариантов: {len(variants)}\n\n"
        "Добавьте ещё вариант или запустите эксперимент:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ExperimentCb.filter(F.action == "add_variant"))
async def cb_add_variant(
    callback: CallbackQuery, callback_data: ExperimentCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(CreateExperiment.waiting_variant_name)
    await state.update_data(bot_id=callback_data.bot_id, exp_id=callback_data.exp_id)
    cancel_kb = InlineKeyboardBuilder()
    cancel_kb.button(
        text="❌ Отмена",
        callback_data=ExperimentCb(action="list", bot_id=callback_data.bot_id),
    )
    await callback.message.edit_text(
        "➕ <b>Добавить вариант</b>\n\nВведите название варианта:",
        parse_mode="HTML",
        reply_markup=cancel_kb.as_markup(),
    )


@router.callback_query(ExperimentCb.filter(F.action == "start"))
async def cb_exp_start(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("A/B тесты", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro", back_callback=BmCb(action="analytics")),
        )
        return
    if not await _owns_experiment(pool, callback_data.exp_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.answer()
    # Guard: don't start if already active
    exp = await db.get_experiment(pool, callback_data.exp_id)
    if not exp:
        await callback.message.edit_text("❌ Эксперимент не найден.", parse_mode="HTML")
        return
    if exp["status"] == "active":
        await callback.message.edit_text(
            "⚠️ Этот эксперимент уже активен.",
            parse_mode="HTML",
            reply_markup=experiment_view_menu(
                callback_data.bot_id, callback_data.exp_id, "active"
            ),
        )
        return
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    if len(variants) < 2:
        await callback.message.edit_text(
            "❌ Нужно минимум 2 варианта для запуска эксперимента.",
            parse_mode="HTML",
            reply_markup=experiment_view_menu(
                callback_data.bot_id, callback_data.exp_id, exp["status"]
            ),
        )
        return
    await db.set_experiment_status(pool, callback_data.exp_id, "active")
    exp = await db.get_experiment(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=experiment_view_menu(
            callback_data.bot_id, callback_data.exp_id, "active"
        ),
    )


@router.callback_query(ExperimentCb.filter(F.action == "pause"))
async def cb_exp_pause(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    if not await _owns_experiment(pool, callback_data.exp_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.answer("⏸ Эксперимент приостановлен.")
    await db.set_experiment_status(pool, callback_data.exp_id, "paused")
    exp = await db.get_experiment(pool, callback_data.exp_id)
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=experiment_view_menu(
            callback_data.bot_id, callback_data.exp_id, "paused"
        ),
    )


@router.callback_query(ExperimentCb.filter(F.action == "resume"))
async def cb_exp_resume(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("A/B тесты", "pro"),
            parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro", back_callback=BmCb(action="analytics")),
        )
        return
    if not await _owns_experiment(pool, callback_data.exp_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.answer("▶️ Эксперимент возобновлён.")
    await db.set_experiment_status(pool, callback_data.exp_id, "active")
    exp = await db.get_experiment(pool, callback_data.exp_id)
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=experiment_view_menu(
            callback_data.bot_id, callback_data.exp_id, "active"
        ),
    )


@router.callback_query(ExperimentCb.filter(F.action == "pick_winner"))
async def cb_pick_winner(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    if not await _owns_experiment(pool, callback_data.exp_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.answer()
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    if not variants:
        await callback.message.edit_text("❌ Нет вариантов.", parse_mode="HTML")
        return
    exp = await db.get_experiment(pool, callback_data.exp_id)
    safe_name = _html_escape(exp["name"])
    await callback.message.edit_text(
        f"🏆 Выберите победителя эксперимента «{safe_name}»:",
        parse_mode="HTML",
        reply_markup=variant_pick_menu(
            callback_data.bot_id, callback_data.exp_id, variants
        ),
    )


@router.callback_query(ExperimentCb.filter(F.action == "set_winner"))
async def cb_set_winner(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    if not await _owns_experiment(pool, callback_data.exp_id, callback.from_user.id):
        await callback.answer("⛔ Нет доступа.", show_alert=True)
        return
    await callback.answer(
        "🏆 Победитель выбран! Эксперимент завершён.", show_alert=True
    )
    await pool.execute(
        "UPDATE experiments SET status='completed', winner_variant_id=$2 WHERE id=$1",
        callback_data.exp_id,
        callback_data.variant_id,
    )
    exp = await db.get_experiment(pool, callback_data.exp_id)
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=experiment_view_menu(
            callback_data.bot_id, callback_data.exp_id, "completed"
        ),
    )
    # Notify the bot owner about experiment completion
    owner_id = await db.get_bot_owner(pool, callback_data.bot_id)
    if owner_id and owner_id != callback.from_user.id:
        winner_variant = next(
            (v for v in variants if v["id"] == callback_data.variant_id), None
        )
        winner_name = _html_escape(winner_variant["name"]) if winner_variant else "—"
        safe_exp_name = _html_escape(exp["name"])
        try:
            await callback.bot.send_message(
                owner_id,
                f"🏆 <b>Эксперимент завершён!</b>\n\n"
                f"Эксперимент: <b>{safe_exp_name}</b>\n"
                f"Победитель: <b>{winner_name}</b>\n\n"
                f"Результаты доступны в разделе A/B Тесты.",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.callback_query(ExperimentCb.filter(F.action == "delete"))
async def cb_exp_delete(
    callback: CallbackQuery, callback_data: ExperimentCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("🗑 Эксперимент удалён.")
    await db.delete_experiment(pool, callback_data.exp_id, callback_data.bot_id)
    exps = await db.get_experiments(pool, callback_data.bot_id)
    if not exps:
        empty_hint = (
            "\n\n💡 <b>Список пуст.</b> Нажмите «➕ Новый эксперимент», "
            "чтобы создать первый A/B тест."
        )
    else:
        empty_hint = ""
    await callback.message.edit_text(
        f"🧪 <b>A/B Эксперименты</b>\n\nВсего: {len(exps)}{empty_hint}",
        parse_mode="HTML",
        reply_markup=experiments_menu(callback_data.bot_id, exps),
    )

"""A/B experiment management."""
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
import asyncpg
from bot.callbacks import ExperimentCb, BotCb
from bot.keyboards import experiments_menu, experiment_view_menu, experiment_type_menu, variant_pick_menu, back_to_bot, subscription_locked_markup
from bot.utils.subscription import require_plan, locked_text
from database import db

router = Router()


class CreateExperiment(StatesGroup):
    waiting_name = State()
    waiting_variant_name = State()
    waiting_variant_content = State()


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _exp_text(exp, variants: list) -> str:
    status_label = {"draft": "📝 Черновик", "active": "🟢 Активен", "paused": "⏸ Пауза", "completed": "✅ Завершён"}
    type_label = {"start_message": "/start сообщение", "auto_reply": "Авто-ответ", "funnel": "Воронка"}
    safe_name = _html_escape(exp['name'])
    lines = [
        f"🧪 <b>Эксперимент: {safe_name}</b>",
        f"Тип: {type_label.get(exp['experiment_type'], _html_escape(exp['experiment_type']))}",
        f"Статус: {status_label.get(exp['status'], _html_escape(exp['status']))}",
        f"Вариантов: {len(variants)}",
        "",
        "<b>Варианты:</b>",
    ]
    for v in variants:
        ctr = round(v["conversions"]/v["impressions"]*100, 1) if v["impressions"] else 0
        winner_mark = " 🏆" if exp.get("winner_variant_id") == v["id"] else ""
        safe_vname = _html_escape(v['name'])
        lines.append(f"  • <b>{safe_vname}{winner_mark}</b>: {v['impressions']} показов, {v['conversions']} конверсий ({ctr}%)")
        lines.append(f"    {_html_escape((v.get('content') or '')[:80])}…")
    return "\n".join(lines)


@router.callback_query(ExperimentCb.filter(F.action == "list"))
async def cb_exp_list(callback: CallbackQuery, callback_data: ExperimentCb,
                       pool: asyncpg.Pool) -> None:

    if not await require_plan(pool, callback.from_user.id, "pro"):
        await callback.answer()
        await callback.message.edit_text(
            locked_text("A/B тесты", "pro"), parse_mode="HTML",
            reply_markup=subscription_locked_markup("pro"),
        )
        return
    row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not row:
        await callback.answer("Бот не найден.", show_alert=True)
        return
    exps = await db.get_experiments(pool, callback_data.bot_id)
    label = f"@{row['username']}" if row["username"] else row["first_name"]
    active = sum(1 for e in exps if e["status"] == "active")
    await callback.message.edit_text(
        f"🧪 <b>A/B Тесты — {label}</b>\n\n"
        "📌 <b>Что это?</b>\n"
        "A/B тест — это когда вы показываете разным пользователям разные версии сообщения и смотрите, какая лучше работает. Например: одним пришёл заголовок «Привет!», другим «Добро пожаловать!» — и вы видите, после какого больше людей остаётся.\n\n"
        "💡 <b>Как использовать:</b>\n"
        "Создайте эксперимент → добавьте 2+ варианта → запустите → через несколько дней выберите победителя.\n\n"
        f"Экспериментов: <b>{len(exps)}</b> | Активных: <b>{active}</b>",
        parse_mode="HTML",
        reply_markup=experiments_menu(callback_data.bot_id, exps),
    )


@router.callback_query(ExperimentCb.filter(F.action == "view"))
async def cb_exp_view(callback: CallbackQuery, callback_data: ExperimentCb,
                       pool: asyncpg.Pool) -> None:

    exp = await db.get_experiment(pool, callback_data.exp_id)
    if not exp:
        await callback.answer("Эксперимент не найден.", show_alert=True)
        return
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=experiment_view_menu(callback_data.bot_id, callback_data.exp_id, exp["status"]),
    )


@router.callback_query(ExperimentCb.filter(F.action == "create"))
async def cb_exp_create(callback: CallbackQuery, callback_data: ExperimentCb,
                         state: FSMContext) -> None:
    await state.set_state(CreateExperiment.waiting_name)
    await state.update_data(bot_id=callback_data.bot_id)
    await callback.message.edit_text(
        "🧪 <b>Новый эксперимент</b>\n\nВыберите тип:",
        parse_mode="HTML",
        reply_markup=experiment_type_menu(callback_data.bot_id),
    )


@router.callback_query(ExperimentCb.filter(F.action.in_({"type_start", "type_reply"})))
async def cb_exp_type(callback: CallbackQuery, callback_data: ExperimentCb,
                       state: FSMContext) -> None:
    exp_type = "start_message" if callback_data.action == "type_start" else "auto_reply"
    await state.update_data(exp_type=exp_type)
    await state.set_state(CreateExperiment.waiting_name)
    await callback.message.edit_text(
        "🧪 <b>Новый эксперимент</b>\n\nВведите название эксперимента:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(CreateExperiment.waiting_name, F.text)
async def msg_exp_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    name = message.text.strip()
    exp_id = await db.create_experiment(pool, data["bot_id"], name, data.get("exp_type", "start_message"))
    await state.update_data(exp_id=exp_id)
    await state.set_state(CreateExperiment.waiting_variant_name)
    safe_name = _html_escape(name)
    await message.answer(
        f"✅ Эксперимент «{safe_name}» создан!\n\n"
        "Добавьте первый вариант.\n"
        "Введите название варианта (например: «Контроль» или «Вариант A»):",
        parse_mode="HTML",
    )


@router.message(CreateExperiment.waiting_variant_name, F.text)
async def msg_variant_name(message: Message, state: FSMContext) -> None:
    variant_name = message.text.strip()
    await state.update_data(variant_name=variant_name)
    await state.set_state(CreateExperiment.waiting_variant_content)
    safe_vname = _html_escape(variant_name)
    await message.answer(
        f"Вариант: <b>{safe_vname}</b>\n\n"
        "Введите содержимое (текст /start сообщения или авто-ответа):",
        parse_mode="HTML",
    )


@router.message(CreateExperiment.waiting_variant_content, F.text)
async def msg_variant_content(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    variant_id = await db.add_experiment_variant(pool, data["exp_id"], data["variant_name"], message.text)
    exp = await db.get_experiment(pool, data["exp_id"])
    variants = await db.get_experiment_variants(pool, data["exp_id"])
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.callbacks import ExperimentCb as EC
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Ещё вариант", callback_data=EC(action="add_variant", bot_id=data["bot_id"], exp_id=data["exp_id"]))
    kb.button(text="▶️ Запустить эксперимент", callback_data=EC(action="start", bot_id=data["bot_id"], exp_id=data["exp_id"]))
    kb.adjust(1)
    await state.clear()
    safe_vname = _html_escape(data['variant_name'])
    await message.answer(
        f"✅ Вариант «{safe_vname}» добавлен!\n"
        f"Всего вариантов: {len(variants)}\n\n"
        "Добавьте ещё вариант или запустите эксперимент:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(ExperimentCb.filter(F.action == "add_variant"))
async def cb_add_variant(callback: CallbackQuery, callback_data: ExperimentCb,
                          state: FSMContext) -> None:
    await state.set_state(CreateExperiment.waiting_variant_name)
    await state.update_data(bot_id=callback_data.bot_id, exp_id=callback_data.exp_id)
    await callback.message.edit_text(
        "➕ <b>Добавить вариант</b>\n\nВведите название варианта:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(ExperimentCb.filter(F.action == "start"))
async def cb_exp_start(callback: CallbackQuery, callback_data: ExperimentCb,
                        pool: asyncpg.Pool) -> None:

    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    if len(variants) < 2:
        await callback.answer("Нужно минимум 2 варианта для запуска.", show_alert=True)
        return
    await db.set_experiment_status(pool, callback_data.exp_id, "active")
    exp = await db.get_experiment(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=experiment_view_menu(callback_data.bot_id, callback_data.exp_id, "active"),
    )
    await callback.answer("✅ Эксперимент запущен!")


@router.callback_query(ExperimentCb.filter(F.action == "pause"))
async def cb_exp_pause(callback: CallbackQuery, callback_data: ExperimentCb,
                        pool: asyncpg.Pool) -> None:

    await db.set_experiment_status(pool, callback_data.exp_id, "paused")
    exp = await db.get_experiment(pool, callback_data.exp_id)
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=experiment_view_menu(callback_data.bot_id, callback_data.exp_id, "paused"),
    )
    await callback.answer("⏸ Эксперимент приостановлен.")


@router.callback_query(ExperimentCb.filter(F.action == "resume"))
async def cb_exp_resume(callback: CallbackQuery, callback_data: ExperimentCb,
                         pool: asyncpg.Pool) -> None:

    await db.set_experiment_status(pool, callback_data.exp_id, "active")
    exp = await db.get_experiment(pool, callback_data.exp_id)
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=experiment_view_menu(callback_data.bot_id, callback_data.exp_id, "active"),
    )
    await callback.answer("▶️ Эксперимент возобновлён.")


@router.callback_query(ExperimentCb.filter(F.action == "pick_winner"))
async def cb_pick_winner(callback: CallbackQuery, callback_data: ExperimentCb,
                          pool: asyncpg.Pool) -> None:

    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    if not variants:
        await callback.answer("Нет вариантов.", show_alert=True)
        return
    exp = await db.get_experiment(pool, callback_data.exp_id)
    safe_name = _html_escape(exp['name'])
    await callback.message.edit_text(
        f"🏆 Выберите победителя эксперимента «{safe_name}»:",
        parse_mode="HTML",
        reply_markup=variant_pick_menu(callback_data.bot_id, callback_data.exp_id, variants),
    )


@router.callback_query(ExperimentCb.filter(F.action == "set_winner"))
async def cb_set_winner(callback: CallbackQuery, callback_data: ExperimentCb,
                         pool: asyncpg.Pool) -> None:

    await pool.execute(
        "UPDATE experiments SET status='completed', winner_variant_id=$2 WHERE id=$1",
        callback_data.exp_id, callback_data.variant_id,
    )
    exp = await db.get_experiment(pool, callback_data.exp_id)
    variants = await db.get_experiment_variants(pool, callback_data.exp_id)
    text = await _exp_text(exp, variants)
    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=experiment_view_menu(callback_data.bot_id, callback_data.exp_id, "completed"),
    )
    await callback.answer("🏆 Победитель выбран! Эксперимент завершён.", show_alert=True)


@router.callback_query(ExperimentCb.filter(F.action == "delete"))
async def cb_exp_delete(callback: CallbackQuery, callback_data: ExperimentCb,
                         pool: asyncpg.Pool) -> None:

    await db.delete_experiment(pool, callback_data.exp_id, callback_data.bot_id)
    exps = await db.get_experiments(pool, callback_data.bot_id)
    await callback.message.edit_text(
        f"🧪 <b>A/B Эксперименты</b>\n\nВсего: {len(exps)}",
        parse_mode="HTML",
        reply_markup=experiments_menu(callback_data.bot_id, exps),
    )
    await callback.answer("🗑 Эксперимент удалён.")

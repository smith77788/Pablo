"""Auto-Funnel UI — automated message sequences for bot audience segments."""

import html
import logging

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AutoFunnelCb, BmCb
from bot.states import AutoFunnelFSM
from database import db
from services import auto_funnel as af_service

log = logging.getLogger(__name__)
router = Router()

_SEGMENTS = {
    "all":         "👥 Все пользователи",
    "new_7d":      "🆕 Новые за 7 дней",
    "new_30d":     "📅 Новые за 30 дней",
    "inactive_30d":"😴 Неактивные 30+ дней",
}


# ── helpers ───────────────────────────────────────────────────────────────────


async def _get_funnel(pool, funnel_id: int, owner_id: int):
    return await pool.fetchrow(
        "SELECT * FROM auto_funnels WHERE id=$1 AND owner_id=$2",
        funnel_id, owner_id,
    )


def _back_to_menu():
    return InlineKeyboardBuilder().button(
        text="◀️ К Auto-Funnel", callback_data=AutoFunnelCb(action="menu")
    ).as_markup()


# ── menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(AutoFunnelCb.filter(F.action == "menu"))
async def cb_af_menu(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    await callback.answer()
    funnels = await pool.fetch(
        "SELECT f.*, b.username AS bot_uname, b.first_name AS bot_name FROM auto_funnels f LEFT JOIN managed_bots b ON b.bot_id = f.bot_id WHERE f.owner_id=$1 ORDER BY f.id",
        callback.from_user.id,
    )
    kb = InlineKeyboardBuilder()
    for f in funnels:
        status = "🟢" if f["enabled"] else "🔴"
        bot_label = html.escape(f["bot_uname"] or f["bot_name"] or f"id{f['bot_id']}")
        seg = _SEGMENTS.get(f["target_segment"], f["target_segment"])
        kb.button(
            text=f"{status} {html.escape(f['name'])} / @{bot_label}",
            callback_data=AutoFunnelCb(action="view", funnel_id=f["id"]),
        )
    kb.button(text="➕ Создать воронку", callback_data=AutoFunnelCb(action="create"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="comms"))
    kb.adjust(1)

    active_runs = await pool.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE r.status='active') AS active_cnt,
               COUNT(*) FILTER (WHERE r.status='completed') AS completed_cnt
        FROM auto_funnel_runs r
        JOIN auto_funnels f ON f.id = r.funnel_id
        WHERE f.owner_id = $1
        """,
        callback.from_user.id,
    )
    a = active_runs["active_cnt"] if active_runs else 0
    c = active_runs["completed_cnt"] if active_runs else 0

    await callback.message.edit_text(
        "⚡ <b>Auto-Funnel</b>\n\n"
        "Автоматические цепочки сообщений для аудитории ваших ботов.\n"
        "Настройте шаги (тексты + задержки), выберите сегмент аудитории и запустите.\n\n"
        f"Воронок: <b>{len(funnels)}</b>  |  В работе: <b>{a}</b>  |  Завершено: <b>{c}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── create ────────────────────────────────────────────────────────────────────


@router.callback_query(AutoFunnelCb.filter(F.action == "create"))
async def cb_af_create(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AutoFunnelFSM.waiting_name)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoFunnelCb(action="menu"))
    await callback.message.edit_text(
        "⚡ <b>Новая Auto-Funnel</b>\n\nВведите название воронки:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AutoFunnelFSM.waiting_name, F.text)
async def msg_af_name(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 100:
        await message.answer("⚠️ Название: 1–100 символов.", parse_mode="HTML")
        return
    await state.update_data(name=name)
    await state.set_state(AutoFunnelFSM.picking_bot)
    bots = await pool.fetch(
        "SELECT bot_id, username, first_name FROM managed_bots WHERE added_by=$1 AND is_active=TRUE ORDER BY bot_id",
        message.from_user.id,
    )
    if not bots:
        await state.clear()
        await message.answer(
            "⚠️ Нет доступных ботов. Сначала добавьте бота.",
            parse_mode="HTML",
            reply_markup=_back_to_menu(),
        )
        return
    kb = InlineKeyboardBuilder()
    for b in bots:
        label = html.escape(b["username"] or b["first_name"] or f"id{b['bot_id']}")
        kb.button(
            text=f"🤖 @{label}",
            callback_data=AutoFunnelCb(action="pick_bot", extra=str(b["bot_id"])),
        )
    kb.button(text="❌ Отмена", callback_data=AutoFunnelCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        "⚡ Выберите бота, который будет отправлять сообщения:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoFunnelCb.filter(F.action == "pick_bot"))
async def cb_af_pick_bot(
    callback: CallbackQuery, callback_data: AutoFunnelCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    try:
        bot_id = int(callback_data.extra)
    except (ValueError, TypeError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    data = await state.get_data()
    name = data.get("name", "Воронка")
    # Pick segment
    kb = InlineKeyboardBuilder()
    for seg, label in _SEGMENTS.items():
        kb.button(
            text=label,
            callback_data=AutoFunnelCb(action="pick_segment", extra=f"{bot_id}:{seg}"),
        )
    kb.button(text="❌ Отмена", callback_data=AutoFunnelCb(action="menu"))
    kb.adjust(1)
    await state.update_data(bot_id=bot_id)
    await callback.message.edit_text(
        f"⚡ <b>{html.escape(name)}</b>\n\nВыберите сегмент аудитории:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoFunnelCb.filter(F.action == "pick_segment"))
async def cb_af_pick_segment(
    callback: CallbackQuery, callback_data: AutoFunnelCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    try:
        bot_id_str, segment = callback_data.extra.split(":", 1)
        bot_id = int(bot_id_str)
    except (ValueError, TypeError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    data = await state.get_data()
    name = data.get("name", "Воронка")
    await state.clear()
    funnel_id = await pool.fetchval(
        "INSERT INTO auto_funnels (owner_id, name, bot_id, target_segment) VALUES ($1,$2,$3,$4) RETURNING id",
        callback.from_user.id, name, bot_id, segment,
    )
    await callback.message.edit_text(
        f"✅ Воронка <b>{html.escape(name)}</b> создана!\n\n"
        "Теперь добавьте шаги (сообщения с задержками).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardBuilder().button(
            text="📝 Добавить шаги", callback_data=AutoFunnelCb(action="steps", funnel_id=funnel_id)
        ).as_markup(),
    )


# ── view ──────────────────────────────────────────────────────────────────────


async def _show_funnel(msg_or_cb, pool, funnel, edit: bool = True) -> None:
    fid = funnel["id"]
    steps = await pool.fetch(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num", fid
    )
    bot_row = await pool.fetchrow("SELECT username, first_name FROM managed_bots WHERE bot_id=$1", funnel["bot_id"])
    bot_label = ""
    if bot_row:
        bot_label = html.escape(bot_row["username"] or bot_row["first_name"] or f"id{funnel['bot_id']}")

    stats = await pool.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE status='active') AS act,
               COUNT(*) FILTER (WHERE status='completed') AS done,
               COUNT(*) FILTER (WHERE status='stopped') AS stop,
               COUNT(*) FILTER (WHERE status='error') AS err
        FROM auto_funnel_runs WHERE funnel_id=$1
        """,
        fid,
    )
    s_act  = stats["act"]  if stats else 0
    s_done = stats["done"] if stats else 0
    s_stop = stats["stop"] if stats else 0
    s_err  = stats["err"]  if stats else 0

    status = "🟢 Активна" if funnel["enabled"] else "🔴 Выключена"
    seg = _SEGMENTS.get(funnel["target_segment"], funnel["target_segment"])

    steps_summary = ""
    for st in steps:
        btn_info = f" [🔗 {html.escape(st['button_text'][:20])}]" if st["button_text"] else ""
        steps_summary += f"\n  Шаг {st['step_num']}: +{st['delay_hours']}ч — {html.escape(st['message_text'][:40])}…{btn_info}"

    text = (
        f"⚡ <b>{html.escape(funnel['name'])}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Бот: @{bot_label}\n"
        f"Сегмент: <b>{seg}</b>\n"
        f"Шагов: <b>{len(steps)}</b>{steps_summary}\n\n"
        f"📊 В работе: {s_act}  ✅ Завершено: {s_done}  ⏹ Остановлено: {s_stop}  ❌ Ошибок: {s_err}"
    )

    kb = InlineKeyboardBuilder()
    toggle = "🔴 Выключить" if funnel["enabled"] else "🟢 Включить"
    kb.button(text=toggle,               callback_data=AutoFunnelCb(action="toggle", funnel_id=fid))
    kb.button(text="📝 Шаги",            callback_data=AutoFunnelCb(action="steps", funnel_id=fid))
    kb.button(text="🚀 Запустить",       callback_data=AutoFunnelCb(action="launch", funnel_id=fid))
    kb.button(text="📊 Статистика",      callback_data=AutoFunnelCb(action="stats", funnel_id=fid))
    kb.button(text="🗑 Удалить",         callback_data=AutoFunnelCb(action="del", funnel_id=fid))
    kb.button(text="◀️ К Auto-Funnel",  callback_data=AutoFunnelCb(action="menu"))
    kb.adjust(2, 2, 1, 1)

    if edit:
        await msg_or_cb.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await msg_or_cb.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(AutoFunnelCb.filter(F.action == "view"))
async def cb_af_view(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    if not funnel:
        await callback.answer("Воронка не найдена.", show_alert=True)
        return
    await _show_funnel(callback.message, pool, funnel)


# ── toggle ────────────────────────────────────────────────────────────────────


@router.callback_query(AutoFunnelCb.filter(F.action == "toggle"))
async def cb_af_toggle(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    if not funnel:
        await callback.answer("Воронка не найдена.", show_alert=True)
        return
    new_state = not funnel["enabled"]
    await pool.execute(
        "UPDATE auto_funnels SET enabled=$1, updated_at=NOW() WHERE id=$2",
        new_state, callback_data.funnel_id,
    )
    await callback.answer("🟢 Включена" if new_state else "🔴 Выключена")
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    await _show_funnel(callback.message, pool, funnel)


# ── steps management ──────────────────────────────────────────────────────────


@router.callback_query(AutoFunnelCb.filter(F.action == "steps"))
async def cb_af_steps(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    if not funnel:
        await callback.answer("Воронка не найдена.", show_alert=True)
        return
    steps = await pool.fetch(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num",
        callback_data.funnel_id,
    )
    kb = InlineKeyboardBuilder()
    for st in steps:
        btn_info = f" [🔗]" if st["button_text"] else ""
        kb.button(
            text=f"Шаг {st['step_num']}: +{st['delay_hours']}ч {html.escape(st['message_text'][:25])}…{btn_info} ✕",
            callback_data=AutoFunnelCb(action="del_step", funnel_id=callback_data.funnel_id, extra=str(st["id"])),
        )
    kb.button(text="➕ Добавить шаг", callback_data=AutoFunnelCb(action="add_step", funnel_id=callback_data.funnel_id))
    kb.button(text="◀️ Назад", callback_data=AutoFunnelCb(action="view", funnel_id=callback_data.funnel_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"📝 <b>Шаги воронки: {html.escape(funnel['name'])}</b>\n\n"
        "Нажмите на шаг чтобы удалить его.\n"
        "Задержка — через сколько часов после запуска воронки отправить этот шаг.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoFunnelCb.filter(F.action == "add_step"))
async def cb_af_add_step(
    callback: CallbackQuery, callback_data: AutoFunnelCb, state: FSMContext
) -> None:
    await callback.answer()
    await state.set_state(AutoFunnelFSM.waiting_step_delay)
    await state.update_data(funnel_id=callback_data.funnel_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AutoFunnelCb(action="steps", funnel_id=callback_data.funnel_id))
    await callback.message.edit_text(
        "➕ <b>Добавить шаг</b>\n\n"
        "Введите задержку в часах (число от 0 до 720).\n"
        "<code>0</code> — отправить сразу при запуске.\n"
        "<code>24</code> — через 24 часа, <code>72</code> — через 3 дня.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(AutoFunnelFSM.waiting_step_delay, F.text)
async def msg_af_step_delay(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("⚠️ Введите целое число (0–720).", parse_mode="HTML")
        return
    delay = int(text)
    if not (0 <= delay <= 720):
        await message.answer("⚠️ Задержка: от 0 до 720 часов.", parse_mode="HTML")
        return
    await state.update_data(step_delay=delay)
    await state.set_state(AutoFunnelFSM.waiting_step_text)
    await message.answer(
        f"✅ Задержка: <b>{delay} ч</b>\n\nТеперь введите текст сообщения для этого шага.\n"
        "Поддерживается HTML разметка: <code>&lt;b&gt;, &lt;i&gt;, &lt;a&gt;</code>",
        parse_mode="HTML",
    )


@router.message(AutoFunnelFSM.waiting_step_text, F.text)
async def msg_af_step_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text or len(text) > 4000:
        await message.answer("⚠️ Текст: 1–4000 символов.", parse_mode="HTML")
        return
    await state.update_data(step_text=text)
    await state.set_state(AutoFunnelFSM.waiting_step_button)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без кнопки", callback_data=AutoFunnelCb(action="step_no_btn"))
    await message.answer(
        "🔘 Добавить кнопку к шагу?\n\n"
        "Введите текст кнопки и URL через символ <code>|</code>, например:\n"
        "<code>Подписаться | https://t.me/mychannel</code>\n\n"
        "Или нажмите «Без кнопки».",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoFunnelCb.filter(F.action == "step_no_btn"))
async def cb_af_step_no_btn(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()
    await state.clear()
    await _save_step(callback.message, pool, data, btn_text=None, btn_url=None, edit=True)


@router.message(AutoFunnelFSM.waiting_step_button, F.text)
async def msg_af_step_button(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    await state.clear()
    text = (message.text or "").strip()
    btn_text, btn_url = None, None
    if "|" in text:
        parts = text.split("|", 1)
        btn_text = parts[0].strip()[:64]
        btn_url = parts[1].strip()[:512]
        if not btn_url.startswith(("http://", "https://", "tg://")):
            await message.answer(
                "⚠️ URL должен начинаться с http://, https:// или tg://",
                parse_mode="HTML",
            )
            return
    await _save_step(message, pool, data, btn_text=btn_text, btn_url=btn_url, edit=False)


async def _save_step(
    msg_or_cb, pool, data: dict, btn_text, btn_url, edit: bool
) -> None:
    funnel_id = data.get("funnel_id")
    delay     = data.get("step_delay", 0)
    text      = data.get("step_text", "")

    max_num = await pool.fetchval(
        "SELECT COALESCE(MAX(step_num), 0) FROM auto_funnel_steps WHERE funnel_id=$1", funnel_id
    )
    new_num = (max_num or 0) + 1
    await pool.execute(
        """
        INSERT INTO auto_funnel_steps (funnel_id, step_num, delay_hours, message_text, button_text, button_url)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        funnel_id, new_num, delay, text, btn_text, btn_url,
    )
    reply_text = f"✅ Шаг {new_num} добавлен (+{delay}ч)."
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Ещё шаг", callback_data=AutoFunnelCb(action="add_step", funnel_id=funnel_id))
    kb.button(text="◀️ К воронке", callback_data=AutoFunnelCb(action="view", funnel_id=funnel_id))
    kb.adjust(1)
    if edit:
        await msg_or_cb.edit_text(reply_text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await msg_or_cb.answer(reply_text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(AutoFunnelCb.filter(F.action == "del_step"))
async def cb_af_del_step(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    try:
        step_id = int(callback_data.extra)
    except (ValueError, TypeError):
        await callback.answer("Ошибка.", show_alert=True)
        return
    await pool.execute(
        "DELETE FROM auto_funnel_steps WHERE id=$1 AND funnel_id=$2",
        step_id, callback_data.funnel_id,
    )
    # Renumber steps
    steps = await pool.fetch(
        "SELECT id FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num", callback_data.funnel_id
    )
    for i, st in enumerate(steps, start=1):
        await pool.execute("UPDATE auto_funnel_steps SET step_num=$1 WHERE id=$2", i, st["id"])
    await callback.answer("🗑 Шаг удалён")
    # Show updated steps list
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    if not funnel:
        await callback.message.edit_text("Воронка не найдена.", reply_markup=_back_to_menu())
        return
    steps = await pool.fetch(
        "SELECT * FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num", callback_data.funnel_id
    )
    kb = InlineKeyboardBuilder()
    for st in steps:
        btn_info = " [🔗]" if st["button_text"] else ""
        kb.button(
            text=f"Шаг {st['step_num']}: +{st['delay_hours']}ч {html.escape(st['message_text'][:25])}…{btn_info} ✕",
            callback_data=AutoFunnelCb(action="del_step", funnel_id=callback_data.funnel_id, extra=str(st["id"])),
        )
    kb.button(text="➕ Добавить шаг", callback_data=AutoFunnelCb(action="add_step", funnel_id=callback_data.funnel_id))
    kb.button(text="◀️ Назад", callback_data=AutoFunnelCb(action="view", funnel_id=callback_data.funnel_id))
    kb.adjust(1)
    await callback.message.edit_reply_markup(reply_markup=kb.as_markup())


# ── launch ────────────────────────────────────────────────────────────────────


@router.callback_query(AutoFunnelCb.filter(F.action == "launch"))
async def cb_af_launch(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    if not funnel:
        await callback.answer("Воронка не найдена.", show_alert=True)
        return
    steps_cnt = await pool.fetchval(
        "SELECT COUNT(*) FROM auto_funnel_steps WHERE funnel_id=$1", callback_data.funnel_id
    )
    if not steps_cnt:
        await callback.answer("Добавьте хотя бы один шаг перед запуском.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for seg, label in _SEGMENTS.items():
        kb.button(
            text=f"▶️ {label}",
            callback_data=AutoFunnelCb(action="launch_confirm", funnel_id=callback_data.funnel_id, extra=seg),
        )
    kb.button(text="◀️ Отмена", callback_data=AutoFunnelCb(action="view", funnel_id=callback_data.funnel_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"🚀 <b>Запуск воронки: {html.escape(funnel['name'])}</b>\n\n"
        "Выберите сегмент аудитории для этого запуска:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoFunnelCb.filter(F.action == "launch_confirm"))
async def cb_af_launch_confirm(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    segment = callback_data.extra
    await callback.message.edit_text("⏳ Запускаю…", parse_mode="HTML")
    try:
        count = await af_service.launch_funnel(
            pool, callback_data.funnel_id, callback.from_user.id, segment
        )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка запуска: {html.escape(str(e)[:200])}",
            parse_mode="HTML",
            reply_markup=_back_to_menu(),
        )
        return
    seg_label = _SEGMENTS.get(segment, segment)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К воронке", callback_data=AutoFunnelCb(action="view", funnel_id=callback_data.funnel_id))
    await callback.message.edit_text(
        f"🚀 <b>Воронка запущена!</b>\n\n"
        f"Сегмент: <b>{seg_label}</b>\n"
        f"Новых пользователей в очереди: <b>{count}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── stats ─────────────────────────────────────────────────────────────────────


@router.callback_query(AutoFunnelCb.filter(F.action == "stats"))
async def cb_af_stats(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    if not funnel:
        await callback.answer("Воронка не найдена.", show_alert=True)
        return
    stats = await pool.fetchrow(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE status='active') AS active,
               COUNT(*) FILTER (WHERE status='completed') AS completed,
               COUNT(*) FILTER (WHERE status='stopped') AS stopped,
               COUNT(*) FILTER (WHERE status='error') AS errors,
               MIN(started_at) AS first_run,
               MAX(started_at) AS last_run
        FROM auto_funnel_runs WHERE funnel_id=$1
        """,
        callback_data.funnel_id,
    )
    steps = await pool.fetch(
        "SELECT step_num, delay_hours FROM auto_funnel_steps WHERE funnel_id=$1 ORDER BY step_num",
        callback_data.funnel_id,
    )
    step_reach = []
    for st in steps:
        cnt = await pool.fetchval(
            "SELECT COUNT(*) FROM auto_funnel_runs WHERE funnel_id=$1 AND (next_step_num > $2 OR status IN ('completed','stopped'))",
            callback_data.funnel_id, st["step_num"],
        )
        step_reach.append(f"  Шаг {st['step_num']} (+{st['delay_hours']}ч): охват {cnt}")

    first = stats["first_run"].strftime("%d.%m.%Y") if stats and stats["first_run"] else "—"
    last  = stats["last_run"].strftime("%d.%m.%Y")  if stats and stats["last_run"]  else "—"
    text = (
        f"📊 <b>Статистика: {html.escape(funnel['name'])}</b>\n\n"
        f"Всего запусков: <b>{stats['total']}</b>\n"
        f"В работе: <b>{stats['active']}</b>\n"
        f"Завершено: <b>{stats['completed']}</b>\n"
        f"Остановлено: <b>{stats['stopped']}</b>\n"
        f"Ошибки: <b>{stats['errors']}</b>\n\n"
        f"Первый: {first}  |  Последний: {last}\n\n"
        + ("\n".join(step_reach) if step_reach else "Нет шагов")
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=AutoFunnelCb(action="view", funnel_id=callback_data.funnel_id))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── delete ────────────────────────────────────────────────────────────────────


@router.callback_query(AutoFunnelCb.filter(F.action == "del"))
async def cb_af_del(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    funnel = await _get_funnel(pool, callback_data.funnel_id, callback.from_user.id)
    if not funnel:
        await callback.answer("Воронка не найдена.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=AutoFunnelCb(action="del_confirm", funnel_id=callback_data.funnel_id))
    kb.button(text="◀️ Отмена", callback_data=AutoFunnelCb(action="view", funnel_id=callback_data.funnel_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚠️ Удалить воронку <b>{html.escape(funnel['name'])}</b>?\n\n"
        "Все шаги и история запусков будут удалены.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AutoFunnelCb.filter(F.action == "del_confirm"))
async def cb_af_del_confirm(
    callback: CallbackQuery, callback_data: AutoFunnelCb, pool: asyncpg.Pool
) -> None:
    await pool.execute(
        "DELETE FROM auto_funnels WHERE id=$1 AND owner_id=$2",
        callback_data.funnel_id, callback.from_user.id,
    )
    await callback.answer("🗑 Удалено")
    await callback.message.edit_text(
        "✅ Воронка удалена.",
        parse_mode="HTML",
        reply_markup=_back_to_menu(),
    )

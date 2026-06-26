"""Ghost Engine UI — autonomous background presence manager for TG accounts."""

import html
import logging
from datetime import datetime, timezone

import asyncpg
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import GhostCb, BmCb
from bot.states import GhostConfigFSM

log = logging.getLogger(__name__)
router = Router()

_PERSONALITY = {
    "ghost":   ("👻 Призрак",       "3–8 действий/день — минимальный след"),
    "watcher": ("👁 Наблюдатель",   "8–15 действий/день — читает, изредка реагирует"),
    "active":  ("🌟 Активный",      "12–25 действий/день — присутствие, реакции, сохранения"),
}

_ACTION_LABELS = {
    "update_status":  "🟢 Онлайн-статус",
    "read_dialogs":   "📖 Прочитал диалоги",
    "react":          "❤️  Реакция на пост",
    "forward_saved":  "💾 Сохранил в избранное",
}

_BACK = lambda: GhostCb(action="menu")


# ── helpers ───────────────────────────────────────────────────────────────────


def _result_icon(r: str) -> str:
    return {"ok": "✅", "skip": "⏭", "error": "❌"}.get(r, "?")


async def _get_profile(pool: asyncpg.Pool, profile_id: int, owner_id: int):
    return await pool.fetchrow(
        "SELECT * FROM ghost_profiles WHERE id = $1 AND owner_id = $2",
        profile_id, owner_id,
    )


async def _get_account_name(pool: asyncpg.Pool, account_id: int) -> str:
    row = await pool.fetchrow(
        "SELECT phone, username, first_name FROM tg_accounts WHERE id = $1",
        account_id,
    )
    if not row:
        return f"id{account_id}"
    return html.escape(
        row["username"] or row["first_name"] or row["phone"] or f"id{account_id}"
    )


# ── menu ──────────────────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "menu"))
async def cb_ghost_menu(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    try:
        profiles = await pool.fetch(
            """
            SELECT gp.*, a.phone, a.username, a.first_name
            FROM ghost_profiles gp
            JOIN tg_accounts a ON a.id = gp.account_id
            WHERE gp.owner_id = $1
            ORDER BY gp.id
            """,
            callback.from_user.id,
        )
    except Exception as e:
        log.error("cb_ghost_menu: DB error: %s", e)
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
        await callback.message.edit_text(
            "👻 <b>Ghost Engine</b>\n\n"
            "⚠️ Модуль недоступен — база данных не содержит нужных таблиц.\n\n"
            "Обратитесь к администратору для применения миграции <code>schema_v105.sql</code>.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for p in profiles:
        name = html.escape(p["username"] or p["first_name"] or p["phone"] or f"id{p['account_id']}")
        status = "🟢" if p["enabled"] else "🔴"
        kb.button(
            text=f"{status} {name} — {_PERSONALITY.get(p['personality'], ('?',))[0]}",
            callback_data=GhostCb(action="view", profile_id=p["id"]),
        )
    kb.button(text="➕ Добавить аккаунт", callback_data=GhostCb(action="add"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="monitoring"))
    kb.adjust(1)
    count = len(profiles)
    active = sum(1 for p in profiles if p["enabled"])
    await callback.message.edit_text(
        "👻 <b>Ghost Engine</b>\n\n"
        "Автономная фоновая активность аккаунтов — онлайн-присутствие, "
        "чтение диалогов, реакции на посты каналов, сохранение в избранное.\n\n"
        "Аккаунты действуют только в рамках существующих подписок. "
        "Никаких новых вступлений, никаких постов в группы.\n\n"
        f"📊 Профилей: <b>{count}</b>  |  Активных: <b>{active}</b>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── add: pick account ─────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "add"))
async def cb_ghost_add(
    callback: CallbackQuery, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    already = {
        r["account_id"]
        for r in await pool.fetch(
            "SELECT account_id FROM ghost_profiles WHERE owner_id = $1",
            callback.from_user.id,
        )
    }
    accounts = await pool.fetch(
        "SELECT id, phone, username, first_name FROM tg_accounts WHERE owner_id = $1 AND banned = FALSE ORDER BY id",
        callback.from_user.id,
    )
    available = [a for a in accounts if a["id"] not in already]
    if not available:
        await callback.message.edit_text(
            "👻 <b>Ghost Engine</b>\n\n"
            "Все ваши аккаунты уже добавлены в Ghost Engine, "
            "или у вас нет активных аккаунтов.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(
                text="◀️ Назад", callback_data=GhostCb(action="menu")
            ).as_markup(),
        )
        return
    kb = InlineKeyboardBuilder()
    for a in available:
        name = html.escape(a["username"] or a["first_name"] or a["phone"] or f"id{a['id']}")
        kb.button(
            text=f"📱 {name}",
            callback_data=GhostCb(action="pick_acc", account_id=a["id"]),
        )
    kb.button(text="◀️ Назад", callback_data=GhostCb(action="menu"))
    kb.adjust(1)
    await callback.message.edit_text(
        "👻 <b>Выберите аккаунт для Ghost Engine</b>\n\n"
        "Аккаунт будет имитировать фоновую активность в Telegram:\n"
        "онлайн-статус, чтение, реакции — только в его текущих подписках.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GhostCb.filter(F.action == "pick_acc"))
async def cb_ghost_pick_acc(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    acc_id = callback_data.account_id
    try:
        await pool.execute(
            """
            INSERT INTO ghost_profiles (owner_id, account_id, personality, daily_cap)
            VALUES ($1, $2, 'ghost', 8)
            ON CONFLICT (owner_id, account_id) DO NOTHING
            """,
            callback.from_user.id, acc_id,
        )
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {html.escape(str(e))}", parse_mode="HTML")
        return
    profile = await pool.fetchrow(
        "SELECT * FROM ghost_profiles WHERE owner_id = $1 AND account_id = $2",
        callback.from_user.id, acc_id,
    )
    if profile:
        await _show_profile(callback.message, pool, profile, callback.from_user.id, edit=True)
    else:
        await callback.message.edit_text(
            "✅ Профиль создан!", parse_mode="HTML",
            reply_markup=InlineKeyboardBuilder().button(
                text="◀️ К Ghost Engine", callback_data=GhostCb(action="menu")
            ).as_markup(),
        )


# ── view profile ──────────────────────────────────────────────────────────────


async def _show_profile(
    message, pool: asyncpg.Pool, profile, owner_id: int, edit: bool = True
) -> None:
    profile_id = profile["id"]
    acc_name = await _get_account_name(pool, profile["account_id"])

    p_label, p_desc = _PERSONALITY.get(profile["personality"], ("?", ""))
    status = "🟢 Активен" if profile["enabled"] else "🔴 Выключен"
    hours = f"{profile['active_hours_start']:02d}:00 – {profile['active_hours_end']:02d}:00 UTC"

    today_cnt = await pool.fetchrow(
        """
        SELECT COUNT(*) AS cnt, COUNT(*) FILTER (WHERE result='ok') AS ok_cnt
        FROM ghost_action_log
        WHERE ghost_profile_id = $1
          AND executed_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
        """,
        profile_id,
    )
    done = today_cnt["cnt"] if today_cnt else 0
    done_ok = today_cnt["ok_cnt"] if today_cnt else 0

    last_row = await pool.fetchrow(
        "SELECT action_type, result, executed_at FROM ghost_action_log WHERE ghost_profile_id=$1 ORDER BY executed_at DESC LIMIT 1",
        profile_id,
    )

    last_txt = "нет"
    if last_row:
        lbl = _ACTION_LABELS.get(last_row["action_type"], last_row["action_type"])
        ico = _result_icon(last_row["result"])
        ts = last_row["executed_at"]
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        last_txt = f"{ico} {lbl}" + (f" ({ts.strftime('%H:%M')})" if ts else "")

    text = (
        f"👻 <b>Ghost Engine — {acc_name}</b>\n\n"
        f"Статус: <b>{status}</b>\n"
        f"Тип: <b>{p_label}</b> — {p_desc}\n"
        f"Окно активности: <b>{hours}</b>\n"
        f"Лимит в сутки: <b>{profile['daily_cap']}</b>  (кулдаун: {profile['cooldown_minutes']} мин)\n\n"
        f"📊 Сегодня: {done} действий, из них ✅ {done_ok}\n"
        f"Последнее: {last_txt}"
    )

    kb = InlineKeyboardBuilder()
    toggle_text = "🔴 Выключить" if profile["enabled"] else "🟢 Включить"
    kb.button(text=toggle_text, callback_data=GhostCb(action="toggle", profile_id=profile_id))
    kb.button(text="🎭 Тип личности", callback_data=GhostCb(action="personality", profile_id=profile_id))
    kb.button(text="⏰ Часы активности", callback_data=GhostCb(action="hours", profile_id=profile_id))
    kb.button(text="🔢 Лимит/кулдаун", callback_data=GhostCb(action="cap", profile_id=profile_id))
    kb.button(text="📋 Логи", callback_data=GhostCb(action="logs", profile_id=profile_id))
    kb.button(text="🗑 Удалить", callback_data=GhostCb(action="del", profile_id=profile_id))
    kb.button(text="◀️ К Ghost Engine", callback_data=GhostCb(action="menu"))
    kb.adjust(2, 2, 2, 1)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


@router.callback_query(GhostCb.filter(F.action == "view"))
async def cb_ghost_view(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if not profile:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    await _show_profile(callback.message, pool, profile, callback.from_user.id)


# ── toggle ────────────────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "toggle"))
async def cb_ghost_toggle(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if not profile:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    new_state = not profile["enabled"]
    await pool.execute(
        "UPDATE ghost_profiles SET enabled=$1, updated_at=NOW() WHERE id=$2",
        new_state, callback_data.profile_id,
    )
    await callback.answer("🟢 Включён" if new_state else "🔴 Выключен")
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    await _show_profile(callback.message, pool, profile, callback.from_user.id)


# ── personality ───────────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "personality"))
async def cb_ghost_personality(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if not profile:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for slug, (label, desc) in _PERSONALITY.items():
        mark = "✅ " if profile["personality"] == slug else ""
        kb.button(
            text=f"{mark}{label}",
            callback_data=GhostCb(action="set_p", profile_id=callback_data.profile_id, extra=slug),
        )
    kb.button(text="◀️ Назад", callback_data=GhostCb(action="view", profile_id=callback_data.profile_id))
    kb.adjust(1)
    lines = "\n".join(f"<b>{l}</b> — {d}" for _, (l, d) in _PERSONALITY.items())
    await callback.message.edit_text(
        "🎭 <b>Выберите тип активности</b>\n\n" + lines,
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GhostCb.filter(F.action == "set_p"))
async def cb_ghost_set_personality(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    slug = callback_data.extra
    if slug not in _PERSONALITY:
        await callback.answer("Неверный тип.", show_alert=True)
        return
    await pool.execute(
        "UPDATE ghost_profiles SET personality=$1, updated_at=NOW() WHERE id=$2 AND owner_id=$3",
        slug, callback_data.profile_id, callback.from_user.id,
    )
    await callback.answer(f"✅ Тип: {_PERSONALITY[slug][0]}")
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if profile:
        await _show_profile(callback.message, pool, profile, callback.from_user.id)


# ── active hours ──────────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "hours"))
async def cb_ghost_hours(
    callback: CallbackQuery, callback_data: GhostCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if not profile:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    await state.set_state(GhostConfigFSM.waiting_hours)
    await state.update_data(profile_id=callback_data.profile_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GhostCb(action="view", profile_id=callback_data.profile_id))
    cur = f"{profile['active_hours_start']:02d}–{profile['active_hours_end']:02d}"
    await callback.message.edit_text(
        f"⏰ <b>Окно активности</b>\n\n"
        f"Текущее: <code>{cur}</code>\n\n"
        "Введите новое окно в формате <code>ЧЧ-ЧЧ</code>, например <code>09-23</code>.\n"
        "Время UTC. Аккаунт будет действовать только в этом промежутке.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(GhostConfigFSM.waiting_hours, F.text)
async def msg_ghost_hours(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    profile_id = data.get("profile_id")
    await state.clear()

    text = (message.text or "").strip()
    parts = text.replace(":", "-").split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await message.answer(
            "⚠️ Неверный формат. Введите как <code>09-23</code>.",
            parse_mode="HTML",
        )
        return
    start, end = int(parts[0]), int(parts[1])
    if not (0 <= start <= 23 and 0 <= end <= 23):
        await message.answer("⚠️ Часы должны быть в диапазоне 0–23.", parse_mode="HTML")
        return
    if start == end:
        await message.answer("⚠️ Начало и конец не должны совпадать.", parse_mode="HTML")
        return

    await pool.execute(
        "UPDATE ghost_profiles SET active_hours_start=$1, active_hours_end=$2, updated_at=NOW() WHERE id=$3 AND owner_id=$4",
        start, end, profile_id, message.from_user.id,
    )
    profile = await _get_profile(pool, profile_id, message.from_user.id)
    if profile:
        await _show_profile(message, pool, profile, message.from_user.id, edit=False)
    else:
        await message.answer("✅ Обновлено.")


# ── daily cap ─────────────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "cap"))
async def cb_ghost_cap(
    callback: CallbackQuery, callback_data: GhostCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if not profile:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    await state.set_state(GhostConfigFSM.waiting_cap)
    await state.update_data(profile_id=callback_data.profile_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GhostCb(action="view", profile_id=callback_data.profile_id))
    await callback.message.edit_text(
        "🔢 <b>Лимит и кулдаун</b>\n\n"
        f"Текущий лимит: <b>{profile['daily_cap']}</b> действий/день\n"
        f"Текущий кулдаун: <b>{profile['cooldown_minutes']}</b> мин между действиями\n\n"
        "Введите два числа через пробел: <code>ЛиМИТ КУЛДАУН</code>\n"
        "Например: <code>12 45</code> — 12 действий в день, 45 минут между ними.\n"
        "Лимит: 1–50. Кулдаун: 10–1440 мин.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.message(GhostConfigFSM.waiting_cap, F.text)
async def msg_ghost_cap(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    profile_id = data.get("profile_id")
    await state.clear()

    parts = (message.text or "").strip().split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await message.answer(
            "⚠️ Введите два числа через пробел: <code>ЛИМИТ КУЛДАУН</code>, напр. <code>12 45</code>.",
            parse_mode="HTML",
        )
        return
    cap, cooldown = int(parts[0]), int(parts[1])
    if not (1 <= cap <= 50):
        await message.answer("⚠️ Лимит должен быть от 1 до 50.", parse_mode="HTML")
        return
    if not (10 <= cooldown <= 1440):
        await message.answer("⚠️ Кулдаун должен быть от 10 до 1440 минут.", parse_mode="HTML")
        return

    await pool.execute(
        "UPDATE ghost_profiles SET daily_cap=$1, cooldown_minutes=$2, updated_at=NOW() WHERE id=$3 AND owner_id=$4",
        cap, cooldown, profile_id, message.from_user.id,
    )
    profile = await _get_profile(pool, profile_id, message.from_user.id)
    if profile:
        await _show_profile(message, pool, profile, message.from_user.id, edit=False)
    else:
        await message.answer("✅ Обновлено.")


# ── logs ──────────────────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "logs"))
async def cb_ghost_logs(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if not profile:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    rows = await pool.fetch(
        """
        SELECT action_type, target, result, error_msg, executed_at
        FROM ghost_action_log
        WHERE ghost_profile_id = $1
        ORDER BY executed_at DESC
        LIMIT 20
        """,
        callback_data.profile_id,
    )
    if not rows:
        text = "👻 <b>Ghost Engine — Лог</b>\n\nДействий пока не было."
    else:
        lines = []
        for r in rows:
            ts = r["executed_at"]
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            t = ts.strftime("%d.%m %H:%M") if ts else "?"
            lbl = _ACTION_LABELS.get(r["action_type"], r["action_type"])
            ico = _result_icon(r["result"])
            tgt = f" → {html.escape(r['target'])}" if r["target"] else ""
            err = f" [{html.escape(r['error_msg'][:40])}]" if r["error_msg"] and r["result"] != "ok" else ""
            lines.append(f"<code>{t}</code> {ico} {lbl}{tgt}{err}")
        text = "👻 <b>Ghost Engine — Последние 20 действий</b>\n\n" + "\n".join(lines)

    acc_name = await _get_account_name(pool, profile["account_id"])
    text = text.replace("Ghost Engine —", f"Ghost Engine — {acc_name} —", 1)

    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GhostCb(action="view", profile_id=callback_data.profile_id))
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── delete ────────────────────────────────────────────────────────────────────


@router.callback_query(GhostCb.filter(F.action == "del"))
async def cb_ghost_del(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    profile = await _get_profile(pool, callback_data.profile_id, callback.from_user.id)
    if not profile:
        await callback.answer("Профиль не найден.", show_alert=True)
        return
    acc_name = await _get_account_name(pool, profile["account_id"])
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Да, удалить", callback_data=GhostCb(action="del_confirm", profile_id=callback_data.profile_id))
    kb.button(text="◀️ Отмена", callback_data=GhostCb(action="view", profile_id=callback_data.profile_id))
    kb.adjust(1)
    await callback.message.edit_text(
        f"⚠️ Удалить Ghost профиль для <b>{acc_name}</b>?\n\n"
        "История действий также будет удалена.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GhostCb.filter(F.action == "del_confirm"))
async def cb_ghost_del_confirm(
    callback: CallbackQuery, callback_data: GhostCb, pool: asyncpg.Pool
) -> None:
    await pool.execute(
        "DELETE FROM ghost_profiles WHERE id=$1 AND owner_id=$2",
        callback_data.profile_id, callback.from_user.id,
    )
    await callback.answer("🗑 Удалено")
    # redirect to menu
    profiles = await pool.fetch(
        "SELECT COUNT(*) AS cnt FROM ghost_profiles WHERE owner_id=$1",
        callback.from_user.id,
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ К Ghost Engine", callback_data=GhostCb(action="menu"))
    await callback.message.edit_text(
        "✅ Ghost профиль удалён.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

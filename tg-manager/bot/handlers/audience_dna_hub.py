"""Audience DNA Hub — deep behavioral profiling UI for bot owners."""

from __future__ import annotations

import html
import logging
from datetime import timezone

import asyncpg
from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BotCb, DnaCb
from database import db
from services import audience_dna as dna_svc
from services.audience_dna import AudienceDNA, generate_recommendations

log = logging.getLogger(__name__)
router = Router()


# ── helpers ───────────────────────────────────────────────────────────────────


def _back_to_dna(bot_id: int) -> "InlineKeyboardMarkup":  # type: ignore[name-defined]
    return (
        InlineKeyboardBuilder()
        .button(text="◀️ К DNA-отчёту", callback_data=DnaCb(action="report", bot_id=bot_id))
        .as_markup()
    )


def _back_to_list() -> "InlineKeyboardMarkup":  # type: ignore[name-defined]
    return (
        InlineKeyboardBuilder()
        .button(text="◀️ К списку ботов", callback_data=DnaCb(action="menu"))
        .as_markup()
    )


def _bot_label(row: asyncpg.Record) -> str:
    return html.escape(
        f"@{row['username']}" if row.get("username")
        else row.get("first_name") or str(row.get("bot_id", ""))
    )


def _format_dna_report(dna: AudienceDNA, bot_name: str) -> str:
    """Render the main DNA report card."""
    peak_h = ", ".join(f"{h}:00" for h in sorted(dna.peak_hours)) if dna.peak_hours else "нет данных"
    peak_d = ", ".join(dna.peak_days) if dna.peak_days else "нет данных"
    content_types = ", ".join(dna.best_content_types[:3]) if dna.best_content_types else "нет данных"
    topics = ", ".join(dna.top_topics[:5]) if dna.top_topics else "нет данных"

    risk_emoji = "🔴" if dna.churn_risk_pct > 40 else "🟡" if dna.churn_risk_pct > 20 else "🟢"
    er_str = f"{dna.avg_engagement_rate:.1f}%" if dna.avg_engagement_rate > 0 else "нет данных"

    computed_str = (
        dna.computed_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
        if dna.computed_at else "—"
    )

    return (
        f"🧬 <b>Audience DNA — {bot_name}</b>\n\n"
        f"⏰ <b>Пик активности (часы):</b> {peak_h}\n"
        f"📆 <b>Пик активности (дни):</b> {peak_d}\n\n"
        f"📝 <b>Лучшие форматы контента:</b>\n{content_types}\n\n"
        f"📈 <b>Средняя вовлечённость:</b> {er_str}\n"
        f"⚠️ <b>Риск оттока:</b> {dna.churn_risk_pct:.0f}% {risk_emoji}\n\n"
        f"🏷️ <b>Топ-темы аудитории:</b>\n{topics}\n\n"
        f"👥 <b>Пользователей проанализировано:</b> {dna.total_users_analyzed:,}\n"
        f"🕐 <b>Актуальность данных:</b> {computed_str}"
    )


# ── Menu: выбор бота ──────────────────────────────────────────────────────────


@router.callback_query(DnaCb.filter(F.action == "menu"))
async def cb_dna_menu(
    callback: CallbackQuery, callback_data: DnaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    bots = await db.get_bots(pool, callback.from_user.id)
    if not bots:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Главное меню", callback_data=BotCb(action="list"))
        await callback.message.edit_text(
            "❌ У вас нет активных ботов.", parse_mode="HTML", reply_markup=kb.as_markup()
        )
        return

    kb = InlineKeyboardBuilder()
    for b in bots:
        label = _bot_label(b)
        kb.button(
            text=f"🤖 {label}",
            callback_data=DnaCb(action="report", bot_id=b["bot_id"]),
        )
    kb.button(text="◀️ Главное меню", callback_data=BotCb(action="list"))
    kb.adjust(1)

    await callback.message.edit_text(
        "🧬 <b>Audience DNA</b>\n\n"
        "Выберите бота для просмотра поведенческого профиля аудитории:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── DNA-отчёт ────────────────────────────────────────────────────────────────


@router.callback_query(DnaCb.filter(F.action == "report"))
async def cb_dna_report(
    callback: CallbackQuery, callback_data: DnaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    bot_row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not bot_row:
        await callback.message.edit_text("❌ Бот не найден.", parse_mode="HTML", reply_markup=_back_to_list())
        return

    bot_name = _bot_label(bot_row)
    try:
        dna = await dna_svc.get_dna(pool, callback_data.bot_id)
    except Exception as e:
        log.error("audience_dna_hub cb_dna_report: %s", e)
        await callback.message.edit_text(
            "🧬 <b>Audience DNA</b>\n\n"
            "⚠️ Модуль недоступен — таблицы не созданы в базе данных.\n\n"
            "Администратору необходимо применить миграцию <code>schema_v119.sql</code>.",
            parse_mode="HTML",
            reply_markup=_back_to_list(),
        )
        return

    kb = InlineKeyboardBuilder()
    if dna:
        kb.button(
            text="💡 Рекомендации",
            callback_data=DnaCb(action="recs", bot_id=callback_data.bot_id),
        )
        kb.button(
            text="📊 История отчётов",
            callback_data=DnaCb(action="history", bot_id=callback_data.bot_id),
        )
    kb.button(
        text="🔄 Пересчитать DNA",
        callback_data=DnaCb(action="compute", bot_id=callback_data.bot_id),
    )
    kb.button(text="◀️ К списку ботов", callback_data=DnaCb(action="menu"))
    kb.adjust(1)

    if not dna:
        await callback.message.edit_text(
            f"🧬 <b>Audience DNA — {bot_name}</b>\n\n"
            "📭 DNA-профиль ещё не вычислен.\n\n"
            "Нажмите <b>«Пересчитать DNA»</b> для первичного анализа аудитории.",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
        return

    text = _format_dna_report(dna, bot_name)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ── Рекомендации ─────────────────────────────────────────────────────────────


@router.callback_query(DnaCb.filter(F.action == "recs"))
async def cb_dna_recs(
    callback: CallbackQuery, callback_data: DnaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    bot_row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not bot_row:
        await callback.message.edit_text("❌ Бот не найден.", parse_mode="HTML", reply_markup=_back_to_list())
        return

    bot_name = _bot_label(bot_row)
    dna = await dna_svc.get_dna(pool, callback_data.bot_id)
    if not dna:
        await callback.message.edit_text(
            "❌ DNA не найден. Сначала запустите пересчёт.",
            parse_mode="HTML",
            reply_markup=_back_to_dna(callback_data.bot_id),
        )
        return

    recs = generate_recommendations(dna)
    rec_text = "\n\n".join(recs) if recs else "Недостаточно данных для рекомендаций."

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ К DNA-отчёту",
        callback_data=DnaCb(action="report", bot_id=callback_data.bot_id),
    )
    kb.adjust(1)

    await callback.message.edit_text(
        f"💡 <b>Рекомендации — {bot_name}</b>\n\n{rec_text}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── Пересчёт DNA вручную ──────────────────────────────────────────────────────


@router.callback_query(DnaCb.filter(F.action == "compute"))
async def cb_dna_compute(
    callback: CallbackQuery, callback_data: DnaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer("⏳ Анализирую аудиторию…", show_alert=False)
    bot_row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not bot_row:
        await callback.message.edit_text("❌ Бот не найден.", parse_mode="HTML", reply_markup=_back_to_list())
        return

    bot_name = _bot_label(bot_row)

    await callback.message.edit_text(
        f"⏳ <b>Вычисляю Audience DNA — {bot_name}…</b>\n\n"
        "Анализирую активность аудитории, вовлечённость и паттерны контента.",
        parse_mode="HTML",
    )

    try:
        dna = await dna_svc.compute_dna(pool, callback_data.bot_id, callback.from_user.id)
    except Exception as exc:
        log.error("cb_dna_compute: error for bot_id=%s: %s", callback_data.bot_id, exc)
        await callback.message.edit_text(
            "❌ Ошибка при вычислении DNA. Попробуйте позже.",
            parse_mode="HTML",
            reply_markup=_back_to_dna(callback_data.bot_id),
        )
        return

    text = _format_dna_report(dna, bot_name)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💡 Рекомендации",
        callback_data=DnaCb(action="recs", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="📊 История отчётов",
        callback_data=DnaCb(action="history", bot_id=callback_data.bot_id),
    )
    kb.button(
        text="🔄 Пересчитать снова",
        callback_data=DnaCb(action="compute", bot_id=callback_data.bot_id),
    )
    kb.button(text="◀️ К списку ботов", callback_data=DnaCb(action="menu"))
    kb.adjust(1)

    await callback.message.edit_text(
        f"✅ <b>DNA обновлён!</b>\n\n{text}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


# ── История DNA-отчётов ───────────────────────────────────────────────────────


@router.callback_query(DnaCb.filter(F.action == "history"))
async def cb_dna_history(
    callback: CallbackQuery, callback_data: DnaCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    bot_row = await db.get_bot(pool, callback_data.bot_id, callback.from_user.id)
    if not bot_row:
        await callback.message.edit_text("❌ Бот не найден.", parse_mode="HTML", reply_markup=_back_to_list())
        return

    bot_name = _bot_label(bot_row)
    history = await dna_svc.get_dna_history(pool, callback_data.bot_id, limit=5)

    if not history:
        await callback.message.edit_text(
            f"📊 <b>История DNA — {bot_name}</b>\n\n"
            "Нет исторических данных.",
            parse_mode="HTML",
            reply_markup=_back_to_dna(callback_data.bot_id),
        )
        return

    lines: list[str] = [f"📊 <b>История DNA — {bot_name}</b>\n"]

    for i, snap in enumerate(history):
        ts = (
            snap.computed_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")
            if snap.computed_at else "—"
        )
        er_str = f"{snap.avg_engagement_rate:.1f}%" if snap.avg_engagement_rate else "—"
        churn_str = f"{snap.churn_risk_pct:.0f}%"
        hours_str = ", ".join(f"{h}:00" for h in sorted(snap.peak_hours)) if snap.peak_hours else "—"
        prefix = "📌 <b>Текущий</b>" if i == 0 else f"#{i + 1}"
        lines.append(
            f"{prefix} — {ts}\n"
            f"   ⏰ Пик: {hours_str}\n"
            f"   📈 ER: {er_str}  ⚠️ Отток: {churn_str}\n"
            f"   👥 Пользователей: {snap.total_users_analyzed:,}"
        )

    # Delta between latest and previous
    if len(history) >= 2:
        curr, prev = history[0], history[1]
        er_delta = dna_svc._delta_str(curr.avg_engagement_rate, prev.avg_engagement_rate, "%")
        churn_delta = dna_svc._delta_str(curr.churn_risk_pct, prev.churn_risk_pct, "%")
        lines.append(
            f"\n📉 <b>Изменения vs предыдущий отчёт:</b>\n"
            f"   Вовлечённость: {er_delta}\n"
            f"   Риск оттока: {churn_delta}"
        )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="◀️ К DNA-отчёту",
        callback_data=DnaCb(action="report", bot_id=callback_data.bot_id),
    )
    kb.adjust(1)

    await callback.message.edit_text(
        "\n\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

"""Bot-паритет: нативный дашборд метрик в боте (/dashboard + пункт «Аналитика»).

Операторская сводка на РЕАЛЬНЫХ данных (аккаунты/операции/аудитория/здоровье)
доступна прямо в боте текстом — без mini-app. Источник цифр тот же, что у экрана
дашборда в приложении (`analytics_dashboard.get_dashboard_stats`), плюс каналы.
"""
from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb

log = logging.getLogger(__name__)
router = Router(name="metrics_dashboard")


async def _build_dashboard(pool: asyncpg.Pool, owner_id: int) -> tuple[str, object]:
    """Собрать текст дашборда из реальных данных + клавиатуру."""
    from services.analytics_dashboard import get_dashboard_stats

    try:
        stats = await get_dashboard_stats(pool, owner_id)
    except Exception as e:
        log.warning("dashboard get_dashboard_stats failed: %s", e)
        stats = {}
    acc = stats.get("accounts", {}) or {}
    ops = stats.get("operations_24h", {}) or {}
    aud = stats.get("audience", {}) or {}
    health = stats.get("health", {}) or {}
    try:
        channels = await pool.fetchval(
            "SELECT COUNT(*) FROM managed_channels WHERE owner_id=$1", owner_id) or 0
    except Exception:
        channels = 0

    ops_total = int(ops.get("total", 0) or 0)
    ops_ok = int(ops.get("success", 0) or 0)
    success_rate = round(ops_ok / ops_total * 100, 1) if ops_total else 0.0

    text = (
        "📈 <b>Дашборд метрик</b>\n\n"
        f"📱 <b>Аккаунты:</b> {int(acc.get('total', 0) or 0)} "
        f"(🟢 {int(acc.get('active', 0) or 0)} активных"
        + (f", 🔴 {int(acc.get('banned', 0) or 0)} бан" if acc.get('banned') else "")
        + (f", 🟠 {int(acc.get('spamblock', 0) or 0)} спамблок" if acc.get('spamblock') else "")
        + ")\n"
        f"📡 <b>Каналы:</b> {int(channels)}\n"
        f"👥 <b>Аудитория ботов:</b> {int(aud.get('total_users', 0) or 0)} "
        f"(+{int(aud.get('new_7d', 0) or 0)} за 7д, +{int(aud.get('new_24h', 0) or 0)} за 24ч)\n\n"
        f"⚡ <b>Операции за 24ч:</b> {ops_total} "
        f"(✅ {ops_ok} · {success_rate}% успех"
        + (f" · ❌ {int(ops.get('failed', 0) or 0)}" if ops.get('failed') else "")
        + (f" · ⏳ {int(ops.get('running', 0) or 0)} в работе" if ops.get('running') else "")
        + ")\n"
        f"❤️ <b>Здоровье:</b> ср. траст {health.get('avg_trust', 0)}"
        + (f" · ⚠️ {int(health.get('accounts_at_risk', 0) or 0)} в зоне риска"
           if health.get('accounts_at_risk') else "")
        + "\n"
    )
    # Паритет с приложением: SEO/гео/пульс карантина (реальные, fail-soft)
    try:
        seo_kw = await pool.fetchval(
            "SELECT COUNT(*) FROM tracked_keywords WHERE owner_id=$1 AND is_active=TRUE",
            owner_id) or 0
    except Exception:
        seo_kw = 0
    try:
        geo_plans = await pool.fetchval(
            "SELECT COUNT(*) FROM global_presence_plans WHERE owner_id=$1", owner_id) or 0
    except Exception:
        geo_plans = 0
    try:
        from services.infra_memory import get_account_health
        pulse = (await get_account_health(pool, owner_id))["summary"]
    except Exception:
        pulse = {}
    try:
        seo_sugg = await pool.fetchval(
            "SELECT COUNT(*) FROM bot_seo_suggestions "
            "WHERE owner_id=$1 AND applied_at IS NULL", owner_id) or 0
    except Exception:
        seo_sugg = 0
    _extra = []
    if seo_kw:
        _extra.append(
            f"🔍 SEO-слова: {int(seo_kw)}"
            + (f" · 💡{int(seo_sugg)} подсказ." if seo_sugg else ""))
    if geo_plans:
        _extra.append(f"🌍 Гео-планы: {int(geo_plans)}")
    if pulse.get("quarantine"):
        _extra.append(f"🚧 карантин: {int(pulse['quarantine'])}")
    if _extra:
        text += " · ".join(_extra) + "\n"

    rev = stats.get("revenue_30d_usd")
    if rev:
        text += f"💰 <b>Доход 30д:</b> ${float(rev):.2f}\n"
    text += "\nℹ️ Полные графики и разделы — в приложении."

    kb = InlineKeyboardBuilder()
    try:
        from config import MINI_APP_URL as _URL
        from bot.handlers.botmother_menu import _valid_mini_app_url
        from aiogram.types import WebAppInfo
        url = _valid_mini_app_url(_URL)
        if url:
            kb.button(text="🌐 Открыть приложение", web_app=WebAppInfo(url=url))
    except Exception:
        pass
    kb.button(text="🔄 Обновить", callback_data=BmCb(action="metrics_dashboard").pack())
    kb.button(text="◀️ Назад", callback_data=BmCb(action="analytics").pack())
    kb.adjust(1)
    return text, kb.as_markup()


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message, pool: asyncpg.Pool) -> None:
    text, kb = await _build_dashboard(pool, message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(BmCb.filter(F.action == "metrics_dashboard"))
async def cb_dashboard(callback: CallbackQuery, pool: asyncpg.Pool) -> None:
    try:
        await callback.answer()
    except Exception:
        pass
    text, kb = await _build_dashboard(pool, callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)

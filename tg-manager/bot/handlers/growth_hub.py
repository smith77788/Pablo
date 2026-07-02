"""Growth Agent — продвижение в нише через постинг в чужие группы.

Поток:
  1. Пользователь задаёт нишу (тематику аудитории)
  2. Пользователь вводит рекламный текст для постинга
  3. Growth Agent ищет группы в нише → вступает → постит промо-текст
  4. Отчёт: сколько групп найдено, posted/failed
"""

from __future__ import annotations

import asyncio
import html
import logging

import asyncpg
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import BmCb, GrowthCb
from bot.states import GrowthAgentFSM
from services import operation_bus

log = logging.getLogger(__name__)
router = Router()

_MAX_PROMO_TEXT = 2000
_MIN_PROMO_TEXT = 10


# ── Helpers ──────────────────────────────────────────────────────────────────

def _menu_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎯 Запустить кампанию", callback_data=GrowthCb(action="create"))
    kb.button(text="📋 История кампаний", callback_data=GrowthCb(action="history"))
    kb.button(text="◀️ Назад", callback_data=BmCb(action="growth"))
    kb.adjust(1)
    return kb.as_markup()


def _cancel_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


def _confirm_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Запустить", callback_data=GrowthCb(action="create"))
    kb.button(text="❌ Отмена", callback_data=GrowthCb(action="menu"))
    kb.adjust(2)
    return kb.as_markup()


def _back_kb() -> object:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Назад", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    return kb.as_markup()


# ── Menu ─────────────────────────────────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "menu"))
async def cb_growth_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(
        "🌱 <b>Growth Agent</b>\n\n"
        "Автоматически находит группы по вашей нише и публикует "
        "рекламный текст, привлекая подписчиков в ваш канал.\n\n"
        "<b>Как работает:</b>\n"
        "1. Вы задаёте нишу (например: «крипто», «фитнес», «недвижимость»)\n"
        "2. Опционально — город/регион для локального таргетинга\n"
        "3. Вы пишете рекламный текст для чужих групп\n"
        "4. Выбираете сколько аккаунтов задействовать\n"
        "5. Агент ищет подходящие группы → вступает → публикует\n\n"
        "⚠️ Используйте аккаунты с хорошим trust score — постинг в группах "
        "требует прогретых аккаунтов.",
        reply_markup=_menu_kb(),
    )


# ── Create campaign — step 1: niche ──────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "create"))
async def cb_growth_create(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    data = await state.get_data()
    # Если уже есть niche и promo_text — это повторное подтверждение
    if data.get("niche") and data.get("promo_text"):
        await _launch_campaign(callback, state, pool)
        return

    await state.set_state(GrowthAgentFSM.waiting_niche)
    await callback.message.edit_text(
        "🌱 <b>Growth Agent — шаг 1/5</b>\n\n"
        "Опишите вашу нишу или целевую аудиторию.\n"
        "<i>Примеры:</i>\n"
        "• <code>криптовалюта трейдинг</code>\n"
        "• <code>фитнес похудение</code>\n"
        "• <code>недвижимость инвестиции</code>\n"
        "• <code>маркетинг для малого бизнеса</code>\n\n"
        "Чем конкретнее — тем лучше подберём группы.",
        reply_markup=_cancel_kb(),
    )


@router.message(GrowthAgentFSM.waiting_niche)
async def on_niche_input(message: Message, state: FSMContext) -> None:
    niche = (message.text or "").strip()
    if len(niche) < 3:
        await message.answer("Ниша слишком короткая. Введите хотя бы 3 символа:")
        return
    if len(niche) > 200:
        await message.answer("Ниша слишком длинная (макс 200 символов):")
        return

    await state.update_data(niche=niche)
    await state.set_state(GrowthAgentFSM.waiting_geo)
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Без гео-таргетинга", callback_data=GrowthCb(action="skip_geo"))
    kb.button(text="❌ Отмена", callback_data=GrowthCb(action="menu"))
    kb.adjust(1)
    await message.answer(
        f"✅ Ниша: <b>{html.escape(niche)}</b>\n\n"
        "🌱 <b>Growth Agent — шаг 2/5</b>\n\n"
        "Хотите ограничить поиск конкретным городом/регионом?\n"
        "Введите его (например: <code>Москва</code>, <code>СПб</code>) "
        "или нажмите «Без гео-таргетинга»:",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(GrowthCb.filter(F.action == "skip_geo"), GrowthAgentFSM.waiting_geo)
async def cb_growth_skip_geo(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(geo="")
    await _go_to_promo_step(callback.message, state, is_edit=True, callback=callback)


@router.message(GrowthAgentFSM.waiting_geo)
async def on_geo_input(message: Message, state: FSMContext) -> None:
    geo = (message.text or "").strip()
    if len(geo) > 60:
        await message.answer("Слишком длинно (макс 60 символов). Введите город/регион:")
        return
    await state.update_data(geo=geo)
    await _go_to_promo_step(message, state, is_edit=False)


async def _go_to_promo_step(
    target: Message, state: FSMContext, is_edit: bool, callback: CallbackQuery | None = None
) -> None:
    await state.set_state(GrowthAgentFSM.waiting_promo_text)
    text = (
        "🌱 <b>Growth Agent — шаг 3/5</b>\n\n"
        "Теперь введите рекламный текст, который будет опубликован "
        "в найденных группах.\n\n"
        "<i>Хороший текст:</i>\n"
        "• Привлекательный заголовок\n"
        "• Краткое описание вашего канала/бота\n"
        "• Призыв к действию со ссылкой (@вашканал или t.me/...)\n\n"
        "<i>Пример:</i>\n"
        "<code>💎 Канал про инвестиции в недвижимость\n"
        "Разборы объектов, расчёты доходности, кейсы.\n"
        "➡️ Подписывайся: @mychannel</code>"
    )
    if is_edit and callback:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=_cancel_kb())


@router.message(GrowthAgentFSM.waiting_promo_text)
async def on_promo_text_input(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    promo_text = (message.text or "").strip()
    if len(promo_text) < _MIN_PROMO_TEXT:
        await message.answer(
            f"Текст слишком короткий (минимум {_MIN_PROMO_TEXT} символов)."
        )
        return
    if len(promo_text) > _MAX_PROMO_TEXT:
        await message.answer(
            f"Текст слишком длинный (максимум {_MAX_PROMO_TEXT} символов)."
        )
        return

    data = await state.get_data()
    from services import content_safety

    _v = await content_safety.enforce(
        pool, message.from_user.id, data.get("niche", ""), promo_text,
        surface="growth_agent",
    )
    if _v.blocked:
        await message.answer(content_safety.REFUSAL_TEXT, parse_mode="HTML")
        return

    await state.update_data(promo_text=promo_text)
    await state.set_state(GrowthAgentFSM.waiting_acc_count)
    total = await _get_acc_count(pool, message.from_user.id)
    await message.answer(
        f"🌱 <b>Growth Agent — шаг 4/5</b>\n\n"
        f"Доступно прогретых аккаунтов: <b>{total}</b>\n"
        "Сколько задействовать для поиска и постинга?\n"
        "Введите число или <code>0</code> — использовать до 3 (безопасный режим по умолчанию):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


async def _get_acc_count(pool: asyncpg.Pool, owner_id: int) -> int:
    try:
        return await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
            "AND session_str IS NOT NULL "
            "AND COALESCE(acc_status,'active') NOT IN ('banned','deactivated','session_expired')",
            owner_id,
        ) or 0
    except Exception:
        return 0


@router.message(GrowthAgentFSM.waiting_acc_count)
async def on_acc_count_input(
    message: Message, state: FSMContext, pool: asyncpg.Pool
) -> None:
    try:
        n = int((message.text or "0").strip())
    except ValueError:
        await message.answer("⚠️ Введите число")
        return
    total = await _get_acc_count(pool, message.from_user.id)
    use = min(n, total) if n > 0 else min(3, total)
    if use == 0:
        await message.answer(
            "⚠️ Нет доступных аккаунтов. Добавьте аккаунты в разделе Аккаунты."
        )
        return

    await state.update_data(acc_count=use)
    await state.set_state(GrowthAgentFSM.confirming)
    data = await state.get_data()
    niche = data.get("niche", "")
    geo = data.get("geo", "")
    promo_text = data.get("promo_text", "")

    geo_line = f"<b>Гео:</b> {html.escape(geo)}\n" if geo else ""
    await message.answer(
        "🌱 <b>Growth Agent — подтверждение</b>\n\n"
        f"<b>Ниша:</b> {html.escape(niche)}\n"
        f"{geo_line}"
        f"<b>Аккаунтов:</b> {use}\n\n"
        f"<b>Рекламный текст:</b>\n{html.escape(promo_text)}\n\n"
        "Агент найдёт до 5 групп в нише и опубликует ваш текст в каждой.\n"
        "⏱ Время выполнения: 5–20 минут\n\n"
        "Запустить?",
        parse_mode="HTML",
        reply_markup=_confirm_kb(),
    )


# ── Launch ────────────────────────────────────────────────────────────────────

async def _launch_campaign(
    callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool
) -> None:
    data = await state.get_data()
    niche: str = data.get("niche", "")
    geo: str = data.get("geo", "")
    promo_text: str = data.get("promo_text", "")
    acc_count: int = int(data.get("acc_count") or 3)
    await state.clear()

    try:
        # op_worker._exec_niche_growth_post hard-caps at 5 groups per run
        # regardless of what's submitted here (safety limit to reduce ban risk) —
        # match that cap so the progress bar denominator isn't misleading.
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "niche_growth_post",
            {
                "niche": niche,
                "geo": geo,
                "promo_text": promo_text,
                "acc_count": acc_count,
                "max_groups": 5,
            },
            total_items=5,
        )
    except Exception as exc:
        log.exception("growth_hub: submit failed: %s", exc)
        await callback.message.edit_text(
            "❌ Не удалось запустить кампанию. Попробуйте позже.",
            reply_markup=_back_kb(),
        )
        return

    await callback.message.edit_text(
        f"✅ <b>Growth Agent запущен</b> (#{op_id})\n\n"
        f"<b>Ниша:</b> {html.escape(niche)}\n\n"
        "Агент ищет группы → вступает → публикует ваш рекламный текст.\n"
        "Результат придёт уведомлением когда кампания завершится.",
        reply_markup=_back_kb(),
    )


# ── History ───────────────────────────────────────────────────────────────────

@router.callback_query(GrowthCb.filter(F.action == "history"))
async def cb_growth_history(
    callback: CallbackQuery, callback_data: GrowthCb, pool: asyncpg.Pool
) -> None:
    await callback.answer()
    rows = await pool.fetch(
        """SELECT id, status, done_items, total_items,
                  params->>'niche' AS niche,
                  created_at, finished_at, error_msg
           FROM operation_queue
           WHERE owner_id=$1 AND op_type='niche_growth_post'
           ORDER BY created_at DESC LIMIT 10""",
        callback.from_user.id,
    )
    if not rows:
        await callback.message.edit_text(
            "🌱 <b>История Growth Agent</b>\n\nКампаний ещё не запускалось.",
            reply_markup=_back_kb(),
        )
        return

    lines = ["🌱 <b>История Growth Agent</b>\n"]
    status_icons = {
        "done": "✅", "failed": "❌", "cancelled": "🚫",
        "pending": "⏳", "running": "🔄", "skipped": "⏭",
    }
    for r in rows:
        icon = status_icons.get(r["status"], "•")
        niche = html.escape(r["niche"] or "—")
        posted = r["done_items"] or 0
        total = r["total_items"] or 0
        date = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else "—"
        lines.append(f"{icon} #{r['id']} | {niche}")
        lines.append(f"   {posted}/{total} групп | {date}")
        if r["status"] == "failed" and r["error_msg"]:
            lines.append(f"   ⚠️ {html.escape(r['error_msg'][:60])}")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=_back_kb(),
    )

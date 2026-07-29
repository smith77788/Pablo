"""AI-комментирование из бота (паритет с mini-app ai_comment_submit).

Раньше AI Commenting (контекстные LLM-комментарии под постами целевых каналов)
запускался только из mini-app. Здесь — тот же op 'ai_comment' через operation_bus:
ввод каналов → ниша → тон → постановка в очередь. Исполнитель (op_worker
_exec_ai_comment) и движок ai_comment_engine уже существуют.
"""
from __future__ import annotations

import logging
import re

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.callbacks import AiCommentCb, BmCb
from bot.utils.op_helpers import safe_answer, terminal_kb
from services import operation_bus

log = logging.getLogger(__name__)
router = Router()

_DEFAULT_ACC_COUNT = 3


class AiCommentFSM(StatesGroup):
    waiting_channels = State()
    waiting_niche = State()


def _cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data=AiCommentCb(action="cancel"))
    return kb.as_markup()


async def _start(target, state: FSMContext) -> None:
    await state.set_state(AiCommentFSM.waiting_channels)
    text = (
        "💬 <b>AI-комментирование</b>\n\n"
        "Контекстные LLM-комментарии под свежими постами целевых каналов "
        "(в их чатах обсуждений).\n\n"
        "Введите каналы через запятую или пробел (@username или ссылки), "
        "до 50 штук:"
    )
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=_cancel_kb())
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=_cancel_kb())


@router.message(Command("ai_comment"))
async def cmd_ai_comment(message: Message, state: FSMContext) -> None:
    await _start(message, state)


@router.callback_query(AiCommentCb.filter(F.action == "open"))
async def cb_ai_comment_open(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await _start(callback, state)


@router.callback_query(AiCommentCb.filter(F.action == "cancel"))
async def cb_ai_comment_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    await state.clear()
    await callback.message.edit_text("AI-комментирование отменено.")


@router.message(AiCommentFSM.waiting_channels, F.text)
async def msg_ai_comment_channels(message: Message, state: FSMContext) -> None:
    raw = re.split(r"[\s,]+", message.text or "")
    channels = [c.strip().lstrip("@") for c in raw if c.strip()][:50]
    if not channels:
        await message.answer("⚠️ Укажите хотя бы один канал:", reply_markup=_cancel_kb())
        return
    await state.update_data(channels=channels)
    await state.set_state(AiCommentFSM.waiting_niche)
    await message.answer(
        f"✅ Каналов: <b>{len(channels)}</b>\n\n"
        "Введите <b>нишу/тему</b> для контекста комментариев "
        "(например «крипта», «фитнес»):",
        parse_mode="HTML",
        reply_markup=_cancel_kb(),
    )


@router.message(AiCommentFSM.waiting_niche, F.text)
async def msg_ai_comment_niche(message: Message, state: FSMContext) -> None:
    niche = (message.text or "").strip()[:200]
    await state.update_data(niche=niche)
    from services.ai_comment_engine import COMMENT_TONES

    kb = InlineKeyboardBuilder()
    _tone_labels = {
        "friendly": "🙂 Дружелюбный",
        "expert": "🎓 Экспертный",
        "question": "❓ Вопрос по теме",
        "support": "👍 Поддержка",
        "neutral": "😐 Нейтральный",
    }
    for tone in COMMENT_TONES:
        kb.button(
            text=_tone_labels.get(tone, tone),
            callback_data=AiCommentCb(action="tone", tone=tone),
        )
    kb.button(text="❌ Отмена", callback_data=AiCommentCb(action="cancel"))
    kb.adjust(2, 2, 1, 1)
    await message.answer(
        "🎭 <b>Тон комментариев</b>\n\nВыберите стиль:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AiCommentCb.filter(F.action == "tone"))
async def cb_ai_comment_tone(
    callback: CallbackQuery, callback_data: AiCommentCb, state: FSMContext, pool: asyncpg.Pool
) -> None:
    await safe_answer(callback)
    data = await state.get_data()
    await state.clear()
    channels = data.get("channels") or []
    niche = data.get("niche") or ""
    from services.ai_comment_engine import COMMENT_TONES

    tone = callback_data.tone if callback_data.tone in COMMENT_TONES else "neutral"
    if not channels:
        await callback.message.edit_text("⚠️ Сессия истекла, начните заново: /ai_comment")
        return

    # Проверка активных аккаунтов (как в mini-app).
    try:
        has_acc = await pool.fetchval(
            "SELECT COUNT(*) FROM tg_accounts WHERE owner_id=$1 AND is_active=TRUE "
            "AND session_str IS NOT NULL",
            callback.from_user.id,
        )
    except Exception:
        has_acc = 0
    if not has_acc:
        await callback.message.edit_text(
            "⚠️ Нет активных аккаунтов — добавьте аккаунт в «📱 Аккаунты».",
            reply_markup=terminal_kb(),
        )
        return

    # Гард безопасности ниши (тот же surface, что в mini-app и в исполнителе).
    from services import content_safety

    verdict = await content_safety.enforce(
        pool, callback.from_user.id, niche or "comment", "", surface="ai_comment"
    )
    if verdict.blocked:
        await callback.message.edit_text(
            "⛔️ Ниша заблокирована политикой платформы. Уточните тему."
        )
        return

    try:
        op_id = await operation_bus.submit(
            pool,
            callback.from_user.id,
            "ai_comment",
            {"channels": channels, "niche": niche, "tone": tone,
             "acc_count": _DEFAULT_ACC_COUNT},
            total_items=len(channels),
        )
    except Exception as exc:
        log.exception("ai_comment submit uid=%s", callback.from_user.id)
        await callback.message.edit_text(f"⚠️ Не удалось поставить в очередь: {str(exc)[:150]}")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Детали операции", callback_data=BmCb(action="op_detail", op_id=op_id))
    kb.button(text="💬 Ещё кампания", callback_data=AiCommentCb(action="open"))
    kb.adjust(1)
    await callback.message.edit_text(
        f"💬 <b>AI-комментирование поставлено в очередь</b>\n\n"
        f"Каналов: <b>{len(channels)}</b>\n"
        f"Тон: <b>{tone}</b>\n"
        f"ID операции: <code>#{op_id}</code>\n\n"
        f"<i>Прогресс: /ops → 📋 Очередь.</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )

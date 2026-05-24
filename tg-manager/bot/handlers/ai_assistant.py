"""AI assistant powered by Claude — isolated per user, tool access to their own bots only."""
from __future__ import annotations
import logging
import asyncpg
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import AiCb
from bot.states import AiChat
from bot.utils.subscription import require_plan
from bot.utils.ai_tools import TOOL_DEFINITIONS, run_tool
from config import ANTHROPIC_API_KEY

router = Router()
log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты AI-ассистент платформы TG Manager для управления сетью Telegram-ботов.
Ты помогаешь пользователю анализировать данные его ботов и давать конкретные рекомендации.
Используй инструменты для получения актуальных данных перед ответом.
Отвечай на русском языке. Будь конкретным и давай actionable советы.
У тебя доступ ТОЛЬКО к данным текущего пользователя — данные других пользователей недоступны."""

_MAX_TURNS = 10  # максимум ходов в одной сессии


async def _call_claude(messages: list, pool: asyncpg.Pool, user_id: int) -> str:
    if not ANTHROPIC_API_KEY:
        return "⚠️ AI-ассистент не настроен. Установите ANTHROPIC_API_KEY в конфиге."
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        current_messages = list(messages)
        for _ in range(5):  # max tool loops
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=current_messages,
            )
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result_text = await run_tool(block.name, block.input, pool, user_id)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        })
                current_messages.append({"role": "assistant", "content": response.content})
                current_messages.append({"role": "user", "content": tool_results})
            else:
                for block in response.content:
                    if hasattr(block, "text"):
                        return block.text
                return "Нет ответа."
    except ImportError:
        return "⚠️ Библиотека anthropic не установлена. Выполните: pip install anthropic"
    except Exception as e:
        log.exception("Claude API error: %s", e)
        return f"⚠️ Ошибка AI: {type(e).__name__}"
    return "Превышен лимит итераций."


@router.message(Command("ai"))
async def cmd_ai(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    if not await require_plan(pool, message.from_user.id, "starter"):
        await message.answer(
            "🔒 <b>AI-ассистент — STARTER</b>\n\n"
            "AI-ассистент доступен с подпиской <b>STARTER</b> и выше.\n\n"
            "Оформить: /subscription",
            parse_mode="HTML",
        )
        return
    await state.set_state(AiChat.chatting)
    await state.update_data(messages=[], turns=0)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Завершить сессию", callback_data=AiCb(action="stop"))
    await message.answer(
        "🤖 <b>AI-ассистент TG Manager</b>\n\n"
        "Задайте вопрос или дайте команду. Например:\n\n"
        "• «Как дела у моих ботов?»\n"
        "• «Проанализируй рост аудитории»\n"
        "• «Какой SEO-score у бота [ID]?»\n"
        "• «Кто самые активные пользователи?»\n\n"
        "Я имею доступ только к вашим данным.",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AiCb.filter(F.action == "start"))
async def cb_ai_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.message.edit_text(
            "🔒 <b>AI-ассистент — STARTER</b>\n\n"
            "Оформите подписку: /subscription",
            parse_mode="HTML",
        )
        return
    await state.set_state(AiChat.chatting)
    await state.update_data(messages=[], turns=0)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Завершить", callback_data=AiCb(action="stop"))
    await callback.message.edit_text(
        "🤖 <b>AI-ассистент запущен</b>\n\n"
        "Напишите ваш вопрос или команду:",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AiCb.filter(F.action == "stop"))
async def cb_ai_stop(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 Новая сессия", callback_data=AiCb(action="start"))
    await callback.message.edit_text(
        "✅ Сессия AI-ассистента завершена.",
        reply_markup=kb.as_markup(),
    )


@router.message(AiChat.chatting, F.text)
async def msg_ai_chat(message: Message, state: FSMContext, pool: asyncpg.Pool) -> None:
    data = await state.get_data()
    messages: list = data.get("messages", [])
    turns: int = data.get("turns", 0)

    if turns >= _MAX_TURNS:
        await state.clear()
        await message.answer(
            "⏳ Лимит сессии достигнут (10 сообщений). Начните новую: /ai",
        )
        return

    messages.append({"role": "user", "content": message.text})
    thinking = await message.answer("🤖 <i>Анализирую...</i>", parse_mode="HTML")

    reply = await _call_claude(messages, pool, message.from_user.id)
    messages.append({"role": "assistant", "content": reply})

    await state.update_data(messages=messages, turns=turns + 1)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Завершить сессию", callback_data=AiCb(action="stop"))
    remaining = _MAX_TURNS - turns - 1
    footer = f"\n\n<i>Осталось сообщений в сессии: {remaining}</i>" if remaining <= 3 else ""

    try:
        await thinking.edit_text(
            f"{reply}{footer}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await message.answer(
            f"{reply}{footer}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

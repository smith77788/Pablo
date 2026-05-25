"""AI-ассистент через OpenRouter — изолирован по user_id, доступ только к данным пользователя."""
from __future__ import annotations
import json
import logging
import os
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
from config import OPENROUTER_MODEL

router = Router()
log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Ты AI-ассистент платформы TG Manager для управления сетью Telegram-ботов. "
    "Ты помогаешь пользователю анализировать данные его ботов и давать конкретные рекомендации. "
    "Используй инструменты для получения актуальных данных перед ответом. "
    "Отвечай только на русском языке. Давай конкретные советы с числами. "
    "У тебя доступ ТОЛЬКО к данным текущего пользователя — данные других пользователей тебе недоступны и ты не можешь их получить."
)

_MAX_TURNS = 100  # практически без ограничений

# Модели для fallback при превышении лимита — первой всегда идёт основная модель
_FALLBACK_MODELS = [
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3-haiku",
    "openai/gpt-4o-mini",
    "google/gemini-flash-1.5",
    "meta-llama/llama-3.1-8b-instruct:free",
]


def _get_api_key() -> str:
    """Всегда читаем из env — не кешируем на уровне модуля."""
    return os.getenv("OPENROUTER_API_KEY", "")


def _get_model() -> str:
    return os.getenv("OPENROUTER_MODEL", "") or OPENROUTER_MODEL or "anthropic/claude-3.5-sonnet"


def _get_models_to_try() -> list[str]:
    primary = _get_model()
    models = [primary] + [m for m in _FALLBACK_MODELS if m != primary]
    return list(dict.fromkeys(models))


def _openai_tools() -> list:
    """Конвертирует tool definitions в формат OpenAI/OpenRouter."""
    result = []
    for t in TOOL_DEFINITIONS:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        })
    return result


async def _call_openrouter(messages: list, pool: asyncpg.Pool, user_id: int) -> str:
    api_key = _get_api_key()
    if not api_key:
        log.error("OPENROUTER_API_KEY is not set in environment")
        return (
            "⚠️ <b>AI-ассистент не настроен</b>\n\n"
            "Переменная <code>OPENROUTER_API_KEY</code> не задана.\n"
            "Добавьте её в настройки Railway и перезапустите сервис."
        )
    try:
        from openai import AsyncOpenAI, RateLimitError
    except ImportError:
        return "⚠️ Библиотека openai не установлена (pip install openai)."

    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )
    current_messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + list(messages)
    tools = _openai_tools()
    primary_model = _get_model()
    models_to_try = _get_models_to_try()

    for model in models_to_try:
        try:
            for _ in range(5):  # макс. 5 итераций с инструментами
                response = await client.chat.completions.create(
                    model=model,
                    messages=current_messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=1500,
                )
                choice = response.choices[0]
                msg = choice.message

                if choice.finish_reason == "tool_calls" and msg.tool_calls:
                    current_messages.append(msg.model_dump())
                    for tc in msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments)
                        except Exception:
                            args = {}
                        result_text = await run_tool(tc.function.name, args, pool, user_id)
                        current_messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })
                else:
                    suffix = f"\n\n<i>Модель: {model}</i>" if model != primary_model else ""
                    return (msg.content or "Нет ответа.") + suffix
            return "Превышен лимит итераций инструментов."

        except RateLimitError:
            log.warning("Rate limit for model %s, trying next", model)
            continue
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "limit" in err_str or "429" in err_str or "quota" in err_str:
                log.warning("Quota/rate error for model %s: %s, trying next", model, e)
                continue
            log.exception("OpenRouter API error with model %s: %s", model, e)
            return f"⚠️ Ошибка AI-ассистента: {type(e).__name__}: {str(e)[:100]}"

    return "⚠️ Все модели недоступны. Попробуйте позже."


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
        "📌 <b>Что это?</b>\n"
        "Умный помощник, который знает все ваши данные и может их анализировать в реальном времени. "
        "Он видит только ваши боты и аудиторию — данные других пользователей ему недоступны.\n\n"
        "💡 <b>Примеры вопросов:</b>\n"
        "• «Как дела у моих ботов?»\n"
        "• «У кого самая большая аудитория?»\n"
        "• «Проанализируй активность за последние 7 дней»\n"
        "• «Дай советы по SEO для моего бота»\n"
        "• «Сколько холодных пользователей нужно реактивировать?»\n\n"
        f"<i>Модель: {_get_model()}</i>",
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
        "Напишите ваш вопрос и я отвечу на основе реальных данных ваших ботов:",
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
        await message.answer("⏳ Начните новую сессию: /ai")
        return

    messages.append({"role": "user", "content": message.text})
    thinking = await message.answer("🤖 <i>Анализирую данные...</i>", parse_mode="HTML")

    reply = await _call_openrouter(messages, pool, message.from_user.id)
    messages.append({"role": "assistant", "content": reply})
    await state.update_data(messages=messages, turns=turns + 1)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Завершить сессию", callback_data=AiCb(action="stop"))
    safe_reply = reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    try:
        await thinking.edit_text(
            safe_reply,
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )
    except Exception:
        await message.answer(
            safe_reply,
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
        )

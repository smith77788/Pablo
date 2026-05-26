"""AI-ассистент через OpenRouter — изолирован по user_id, доступ только к данным пользователя.

READ-инструменты возвращают данные для анализа.
ACTION-инструменты запрашивают подтверждение у пользователя перед выполнением.
"""
from __future__ import annotations
import json
import logging
import os
import asyncpg
import aiohttp
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.callbacks import AiCb
from bot.states import AiChat
from bot.utils.subscription import require_plan
from bot.utils.ai_tools import TOOL_DEFINITIONS, run_tool, execute_action
from config import OPENROUTER_MODEL

router = Router()
log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Ты AI-ассистент платформы TG Manager для управления Telegram-ботами и каналами. "
    "Ты можешь АНАЛИЗИРОВАТЬ данные пользователя И ВЫПОЛНЯТЬ задачи управления.\n\n"
    "ВОЗМОЖНОСТИ:\n"
    "- Просматривать данные ботов, аудиторию, статистику\n"
    "- Запускать рассылки для ботов (launch_broadcast)\n"
    "- Обновлять профиль ботов: имя, описание (update_bot_profile)\n"
    "- Публиковать посты в каналы через подключённые аккаунты (post_to_channel)\n\n"
    "ВАЖНО:\n"
    "- Когда вызываешь action-инструмент (launch_broadcast, update_bot_profile, post_to_channel), "
    "действие НЕ выполнится сразу — пользователь увидит запрос на подтверждение.\n"
    "- Сообщи пользователю, что ты подготовил действие и ждёшь подтверждения.\n"
    "- Отвечай только на русском языке.\n"
    "- Давай конкретные советы с числами.\n"
    "- У тебя доступ ТОЛЬКО к данным текущего пользователя — данные других пользователей тебе недоступны."
)

_MAX_TURNS = 100

_FALLBACK_MODELS = [
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "anthropic/claude-3.5-sonnet",
    "google/gemini-flash-1.5",
    "meta-llama/llama-3.1-8b-instruct:free",
]


def _get_api_key() -> str:
    return os.getenv("OPENROUTER_API_KEY", "")


def _get_model() -> str:
    return os.getenv("OPENROUTER_MODEL", "") or OPENROUTER_MODEL or "openai/gpt-4o-mini"


def _get_models_to_try() -> list[str]:
    primary = _get_model()
    models = [primary] + [m for m in _FALLBACK_MODELS if m != primary]
    return list(dict.fromkeys(models))


def _openai_tools() -> list:
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


async def _call_openrouter(
    messages: list,
    pool: asyncpg.Pool,
    user_id: int,
    http: aiohttp.ClientSession | None = None,
) -> str | dict:
    """Returns either a str (AI response) or dict {"__pending__": True, "action_data": {...}, "ai_message": "..."}"""
    api_key = _get_api_key()
    if not api_key:
        return (
            "⚠️ <b>AI-ассистент не настроен</b>\n\n"
            "Переменная <code>OPENROUTER_API_KEY</code> не задана.\n"
            "Добавьте её в настройки Railway и перезапустите сервис."
        )
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return "⚠️ Библиотека openai не установлена (pip install openai)."

    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=60.0,
    )
    base_messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + list(messages)
    tools = _openai_tools()
    primary_model = _get_model()
    models_to_try = _get_models_to_try()

    for model in models_to_try:
        current_messages = list(base_messages)
        try:
            pending_action_data = None
            for _ in range(8):
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
                    assistant_msg: dict = {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                    current_messages.append(assistant_msg)

                    for tc in msg.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments)
                        except Exception:
                            args = {}
                        result_text = await run_tool(tc.function.name, args, pool, user_id, http)

                        # Detect pending action — intercept before AI sees it
                        try:
                            parsed = json.loads(result_text)
                            if isinstance(parsed, dict) and "pending_action" in parsed:
                                pending_action_data = parsed
                                # Tell AI the action is queued for confirmation
                                result_text = json.dumps({
                                    "status": "pending_confirmation",
                                    "message": f"Действие «{parsed['pending_action']}» поставлено в очередь на подтверждение пользователем.",
                                    "preview": parsed.get("preview", ""),
                                }, ensure_ascii=False)
                        except Exception:
                            pass

                        current_messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })
                else:
                    ai_reply = msg.content or "Нет ответа."
                    suffix = f"\n\n<i>Модель: {model}</i>" if model != primary_model else ""
                    full_reply = ai_reply + suffix

                    if pending_action_data:
                        return {
                            "__pending__": True,
                            "action_data": pending_action_data,
                            "ai_message": full_reply,
                        }
                    return full_reply

            return "Превышен лимит итераций инструментов."

        except Exception as e:
            err_str = str(e).lower()
            should_retry = any(x in err_str for x in (
                "rate", "limit", "429", "quota", "overloaded", "timeout",
                "model_not_found", "not found", "404", "invalid model",
            ))
            if should_retry:
                log.warning("Model %s failed (%s), trying next", model, type(e).__name__)
                continue
            log.exception("OpenRouter API error with model %s: %s", model, e)
            return f"⚠️ Ошибка AI-ассистента ({model}): {type(e).__name__}: {str(e)[:120]}"

    return "⚠️ Все модели недоступны. Попробуйте позже или смените OPENROUTER_MODEL."


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
        "📌 <b>Что умею:</b>\n"
        "• Анализировать данные ваших ботов и аудитории\n"
        "• Давать рекомендации по росту и SEO\n"
        "• <b>Запускать рассылки</b> по вашей аудитории\n"
        "• <b>Обновлять профиль</b> ботов (имя, описание)\n"
        "• <b>Публиковать посты</b> в ваши каналы\n\n"
        "💡 <b>Примеры задач:</b>\n"
        "• «Запусти рассылку для @mybot: Привет! Новые функции уже здесь»\n"
        "• «Измени имя бота [id] на «Мой Магазин»»\n"
        "• «Опубликуй в моём канале: Сегодня акция!»\n"
        "• «Как дела у моих ботов?»\n"
        "• «Сколько холодных пользователей нужно реактивировать?»\n\n"
        "⚠️ <i>Все действия требуют вашего подтверждения.</i>\n\n"
        f"<i>Модель: {_get_model()}</i>",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AiCb.filter(F.action == "start"))
async def cb_ai_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "starter"):
        await callback.message.edit_text(
            "🔒 <b>AI-ассистент — STARTER</b>\n\nОформите подписку: /subscription",
            parse_mode="HTML",
        )
        return
    await state.set_state(AiChat.chatting)
    await state.update_data(messages=[], turns=0)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Завершить", callback_data=AiCb(action="stop"))
    await callback.message.edit_text(
        "🤖 <b>AI-ассистент запущен</b>\n\n"
        "Задайте вопрос или поставьте задачу. Я могу анализировать данные ваших ботов, "
        "запускать рассылки, обновлять профили и публиковать посты в каналы.\n\n"
        "⚠️ Любое действие потребует вашего подтверждения.",
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


@router.callback_query(AiCb.filter(F.action == "confirm_action"))
async def cb_ai_confirm_action(
    callback: CallbackQuery, state: FSMContext,
    pool: asyncpg.Pool, http: aiohttp.ClientSession,
) -> None:
    await callback.answer()
    data = await state.get_data()
    action_data = data.get("pending_action_data")
    if not action_data:
        await callback.message.edit_text("⚠️ Данные действия устарели. Попросите ассистента повторить.")
        return

    await callback.message.edit_text("⏳ <i>Выполняю...</i>", parse_mode="HTML")
    result = await execute_action(action_data, pool, callback.from_user.id, http)

    await state.update_data(pending_action_data=None)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Завершить сессию", callback_data=AiCb(action="stop"))
    await callback.message.edit_text(
        f"<b>Результат выполнения:</b>\n\n{result}",
        parse_mode="HTML",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(AiCb.filter(F.action == "cancel_action"))
async def cb_ai_cancel_action(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Действие отменено")
    await state.update_data(pending_action_data=None)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Завершить сессию", callback_data=AiCb(action="stop"))
    await callback.message.edit_text(
        "❌ Действие отменено. Можете задать новый вопрос или задачу.",
        reply_markup=kb.as_markup(),
    )


@router.message(AiChat.chatting, F.text)
async def msg_ai_chat(
    message: Message, state: FSMContext,
    pool: asyncpg.Pool, http: aiohttp.ClientSession,
) -> None:
    data = await state.get_data()
    messages: list = data.get("messages", [])
    turns: int = data.get("turns", 0)

    if turns >= _MAX_TURNS:
        await state.clear()
        await message.answer("⏳ Начните новую сессию: /ai")
        return

    messages.append({"role": "user", "content": message.text})
    thinking = await message.answer("🤖 <i>Анализирую...</i>", parse_mode="HTML")

    reply = await _call_openrouter(messages, pool, message.from_user.id, http)

    if isinstance(reply, dict) and reply.get("__pending__"):
        # AI wants to perform an action — show confirmation
        action_data = reply["action_data"]
        ai_message = reply["ai_message"]
        preview = action_data.get("preview", "")

        await state.update_data(
            messages=messages + [{"role": "assistant", "content": ai_message}],
            turns=turns + 1,
            pending_action_data=action_data,
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтвердить", callback_data=AiCb(action="confirm_action"))
        kb.button(text="❌ Отмена", callback_data=AiCb(action="cancel_action"))
        kb.adjust(2)

        confirm_text = (
            f"{ai_message}\n\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Требуется подтверждение:</b>\n"
            f"<code>{preview}</code>"
        )
        try:
            await thinking.edit_text(confirm_text, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            await message.answer(confirm_text, parse_mode="HTML", reply_markup=kb.as_markup())
    else:
        # Regular AI response (text)
        str_reply = reply if isinstance(reply, str) else str(reply)
        messages.append({"role": "assistant", "content": str_reply})
        await state.update_data(messages=messages, turns=turns + 1)

        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Завершить сессию", callback_data=AiCb(action="stop"))
        try:
            await thinking.edit_text(str_reply, parse_mode="HTML", reply_markup=kb.as_markup())
        except Exception:
            try:
                await thinking.edit_text(str_reply, reply_markup=kb.as_markup())
            except Exception:
                await message.answer(str_reply, parse_mode="HTML", reply_markup=kb.as_markup())

"""AI-ассистент через OpenRouter — изолирован по user_id, доступ только к данным пользователя.

READ-инструменты возвращают данные для анализа.
ACTION-инструменты запрашивают подтверждение у пользователя перед выполнением.
"""
from __future__ import annotations
import json
import logging
import os
from html import escape
from io import BytesIO
from pathlib import Path
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
    "Ты AI-ассистент платформы BotMother OS — корпоративной системы управления Telegram-инфраструктурой.\n"
    "Ты можешь АНАЛИЗИРОВАТЬ данные пользователя И РЕАЛЬНО ВЫПОЛНЯТЬ задачи управления.\n\n"
    "ВОЗМОЖНОСТИ (инструменты):\n"
    "READ — данные:\n"
    "- get_my_bots: список ботов + статистика\n"
    "- get_bot_details: детали конкретного бота\n"
    "- get_network_stats: сводная статистика сети\n"
    "- get_audience_activity: сегментация аудитории (hot/warm/cold/lost)\n"
    "- get_growth_trend: динамика роста за N дней\n"
    "- get_seo_recommendations: SEO-оценка бота\n"
    "- get_my_accounts: список Telegram-аккаунтов\n"
    "- get_my_channels: список каналов из кэша\n"
    "- get_broadcast_history: история рассылок\n\n"
    "ACTION — действия (требуют подтверждения пользователя):\n"
    "- create_channel: СОЗДАТЬ канал или группу в Telegram через подключённый аккаунт\n"
    "- post_to_channel: опубликовать пост в канал\n"
    "- launch_broadcast: запустить рассылку боту сейчас\n"
    "- schedule_broadcast: запланировать рассылку на через N минут\n"
    "- update_bot_profile: обновить имя/описание бота\n\n"
    "ПРОТОКОЛ ДЕЙСТВИЙ:\n"
    "1. Когда пользователь просит создать канал/группу/сеть присутствия — используй create_channel\n"
    "2. Для создания нескольких объектов — вызывай create_channel НЕСКОЛЬКО РАЗ (по одному на каждый)\n"
    "3. Действие НЕ выполнится сразу — пользователь подтвердит его\n"
    "4. После подготовки действия — сообщи что подготовлено и ждёт подтверждения\n"
    "5. НЕ ГОВОРИ что не можешь создать присутствие — ты УМЕЕШЬ создавать каналы и группы\n\n"
    "ВАЖНО:\n"
    "- Отвечай только на русском языке\n"
    "- Давай конкретные советы с числами\n"
    "- Всегда предлагай реальные действия, а не только рекомендации\n"
    "- Доступ только к данным текущего пользователя"
)

_MAX_TURNS = 100
_DEFAULT_MAX_FILE_BYTES = 1_048_576
_DEFAULT_MAX_FILE_CHARS = 60_000
_TELEGRAM_TEXT_LIMIT = 3900
_SUPPORTED_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx",
    ".py", ".sql", ".toml", ".ini", ".env", ".log", ".prisma", ".sh", ".ps1",
}
_SUPPORTED_STRUCTURED_EXTENSIONS = {".pdf", ".docx", ".xlsx"}
_SUPPORTED_FILE_HINT = "txt, md, csv, json, yaml, код, log, pdf, docx, xlsx"

_FALLBACK_MODELS = [
    "anthropic/claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5",
    "openai/gpt-4o-mini",
    "openai/gpt-4o",
    "google/gemini-flash-1.5",
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
]


def _get_positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default
    return max(1024, value)


_MAX_FILE_BYTES = _get_positive_int_env("AI_FILE_MAX_BYTES", _DEFAULT_MAX_FILE_BYTES)
_MAX_FILE_CHARS = _get_positive_int_env("AI_FILE_MAX_CHARS", _DEFAULT_MAX_FILE_CHARS)


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


def _truncate_file_text(text: str) -> str:
    text = text.replace("\x00", "").strip()
    if len(text) <= _MAX_FILE_CHARS:
        return text
    return (
        text[:_MAX_FILE_CHARS].rstrip()
        + "\n\n[Файл длинный, поэтому дальше текст обрезан. Пришлите нужную часть отдельно, если нужны детали ниже.]"
    )


def _decode_text_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("Для чтения PDF нужна библиотека pypdf.") from exc

    reader = PdfReader(BytesIO(raw))
    parts: list[str] = []
    max_pages = 20
    for index in range(min(len(reader.pages), max_pages)):
        page_number = index + 1
        page = reader.pages[index]
        page_text = page.extract_text() or ""
        if page_text.strip():
            parts.append(f"[Страница {page_number}]\n{page_text.strip()}")
    if len(reader.pages) > max_pages:
        parts.append(f"[Показаны первые {max_pages} страниц из {len(reader.pages)}.]")
    return "\n\n".join(parts)


def _extract_docx_text(raw: bytes) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError("Для чтения DOCX нужна библиотека python-docx.") from exc

    doc = Document(BytesIO(raw))
    parts: list[str] = []
    for paragraph in doc.paragraphs:
        text = paragraph.text.strip()
        if text:
            parts.append(text)
    for table_index, table in enumerate(doc.tables, start=1):
        rows: list[str] = []
        for row in table.rows[:100]:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                rows.append(" | ".join(cells))
        if rows:
            parts.append(f"[Таблица {table_index}]\n" + "\n".join(rows))
    return "\n\n".join(parts)


def _extract_xlsx_text(raw: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Для чтения XLSX нужна библиотека openpyxl.") from exc

    workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    parts: list[str] = []
    max_sheets = 5
    max_rows = 120
    max_columns = 20
    try:
        for sheet in workbook.worksheets[:max_sheets]:
            rows: list[str] = []
            for row in sheet.iter_rows(max_row=max_rows, max_col=max_columns, values_only=True):
                values = ["" if value is None else str(value) for value in row]
                if any(value.strip() for value in values):
                    rows.append("\t".join(values).rstrip())
            if rows:
                parts.append(f"[Лист: {sheet.title}]\n" + "\n".join(rows))
        if len(workbook.worksheets) > max_sheets:
            parts.append(f"[Показаны первые {max_sheets} листов из {len(workbook.worksheets)}.]")
    finally:
        workbook.close()
    return "\n\n".join(parts)


def _extract_document_text(file_name: str, mime_type: str | None, raw: bytes) -> str:
    suffix = Path(file_name).suffix.lower()
    mime_type = (mime_type or "").lower()

    if suffix in _SUPPORTED_TEXT_EXTENSIONS or mime_type.startswith("text/"):
        return _decode_text_bytes(raw)
    if suffix == ".pdf":
        return _extract_pdf_text(raw)
    if suffix == ".docx":
        return _extract_docx_text(raw)
    if suffix == ".xlsx":
        return _extract_xlsx_text(raw)

    supported = sorted(_SUPPORTED_TEXT_EXTENSIONS | _SUPPORTED_STRUCTURED_EXTENSIONS)
    raise ValueError(f"Формат пока не поддерживается. Поддерживаются: {', '.join(supported)}")


async def _read_attached_document(message: Message) -> tuple[str, str]:
    document = message.document
    if not document:
        raise ValueError("Файл не найден в сообщении.")

    file_name = document.file_name or "file"
    file_size = document.file_size or 0
    if file_size > _MAX_FILE_BYTES:
        size_mb = _MAX_FILE_BYTES / 1024 / 1024
        raise ValueError(f"Файл слишком большой. Сейчас можно отправлять файлы до {size_mb:.1f} МБ.")

    if message.bot is None:
        raise RuntimeError("Бот не готов скачать файл. Попробуйте отправить файл еще раз.")

    bot_file = await message.bot.get_file(document.file_id)
    downloaded = await message.bot.download_file(bot_file.file_path)
    if hasattr(downloaded, "seek"):
        downloaded.seek(0)
    raw = downloaded.read() if hasattr(downloaded, "read") else bytes(downloaded or b"")
    if len(raw) > _MAX_FILE_BYTES:
        size_mb = _MAX_FILE_BYTES / 1024 / 1024
        raise ValueError(f"Файл слишком большой. Сейчас можно отправлять файлы до {size_mb:.1f} МБ.")

    text = _extract_document_text(file_name, document.mime_type, raw)
    text = _truncate_file_text(text)
    if not text:
        raise ValueError("В файле не удалось найти читаемый текст.")
    return file_name, text


def _build_file_prompt(file_name: str, file_text: str, caption: str | None) -> str:
    task = caption.strip() if caption and caption.strip() else "Изучи файл и помоги по его содержимому."
    return (
        f"{task}\n\n"
        f"Пользователь отправил файл: {file_name}\n"
        "Проанализируй содержимое файла. Если в файле есть ошибки, риски или важные выводы, объясни их простыми словами.\n\n"
        f"--- НАЧАЛО ФАЙЛА: {file_name} ---\n"
        f"{file_text}\n"
        f"--- КОНЕЦ ФАЙЛА: {file_name} ---"
    )


def _split_telegram_text(text: str) -> list[str]:
    if len(text) <= _TELEGRAM_TEXT_LIMIT:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _TELEGRAM_TEXT_LIMIT:
            chunks.append(remaining)
            break

        split_at = max(
            remaining.rfind("\n\n", 0, _TELEGRAM_TEXT_LIMIT),
            remaining.rfind("\n", 0, _TELEGRAM_TEXT_LIMIT),
            remaining.rfind(" ", 0, _TELEGRAM_TEXT_LIMIT),
        )
        if split_at < _TELEGRAM_TEXT_LIMIT // 2:
            split_at = _TELEGRAM_TEXT_LIMIT

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return [chunk for chunk in chunks if chunk]


async def _edit_or_answer_long(
    thinking_message: Message,
    source_message: Message,
    text: str,
    reply_markup=None,
) -> None:
    chunks = _split_telegram_text(text)
    if len(chunks) == 1:
        try:
            await thinking_message.edit_text(chunks[0], parse_mode="HTML", reply_markup=reply_markup)
            return
        except Exception:
            try:
                await thinking_message.edit_text(chunks[0], parse_mode=None, reply_markup=reply_markup)
                return
            except Exception:
                await source_message.answer(chunks[0], parse_mode=None, reply_markup=reply_markup)
                return

    try:
        await thinking_message.edit_text(chunks[0], parse_mode=None)
    except Exception:
        await source_message.answer(chunks[0], parse_mode=None)

    for chunk in chunks[1:-1]:
        await source_message.answer(chunk, parse_mode=None)

    await source_message.answer(chunks[-1], parse_mode=None, reply_markup=reply_markup)


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
    # (model, max_tokens) pairs — при 402 пробуем меньше токенов у той же модели
    attempts: list[tuple[str, int]] = [(m, 1500) for m in models_to_try]
    attempts += [(m, 600) for m in models_to_try]  # fallback с меньшим контекстом

    seen: set[tuple[str, int]] = set()

    for model, max_tokens in attempts:
        if (model, max_tokens) in seen:
            continue
        seen.add((model, max_tokens))
        current_messages = list(base_messages)
        try:
            pending_action_data = None
            for _ in range(8):
                response = await client.chat.completions.create(
                    model=model,
                    messages=current_messages,
                    tools=tools,
                    tool_choice="auto",
                    max_tokens=max_tokens,
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
                "402", "credits", "payment", "billing", "insufficient",
            ))
            if should_retry:
                log.warning("Model %s (max_tokens=%d) failed (%s), trying next", model, max_tokens, type(e).__name__)
                continue
            log.exception("OpenRouter API error with model %s: %s", model, e)
            return f"⚠️ Ошибка AI-ассистента ({model}): {type(e).__name__}: {str(e)[:120]}"

    return (
        "⚠️ <b>AI-ассистент временно недоступен</b>\n\n"
        "Все модели недоступны. Возможные причины:\n"
        "• Недостаточно кредитов на OpenRouter — пополните счёт\n"
        "• Временная перегрузка серверов — попробуйте через несколько минут\n"
        "• Неверный ключ <code>OPENROUTER_API_KEY</code>\n\n"
        "Сменить модель можно через Admin → ⚙️ Системный режим."
    )


async def _process_ai_turn(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    http: aiohttp.ClientSession,
    user_content: str,
    thinking_text: str = "🤖 <i>Разбираюсь...</i>",
) -> None:
    data = await state.get_data()
    messages: list = data.get("messages", [])
    turns: int = data.get("turns", 0)

    if turns >= _MAX_TURNS:
        await state.clear()
        await message.answer("Диалог стал слишком длинным. Начните новую сессию: /ai")
        return

    messages.append({"role": "user", "content": user_content})
    thinking = await message.answer(thinking_text, parse_mode="HTML")

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
            f"⚡ <b>Нужно подтверждение:</b>\n"
            f"<code>{preview}</code>"
        )
        await _edit_or_answer_long(thinking, message, confirm_text, reply_markup=kb.as_markup())
    else:
        # Regular AI response (text)
        str_reply = reply if isinstance(reply, str) else str(reply)
        messages.append({"role": "assistant", "content": str_reply})
        await state.update_data(messages=messages, turns=turns + 1)

        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Завершить сессию", callback_data=AiCb(action="stop"))
        await _edit_or_answer_long(thinking, message, str_reply, reply_markup=kb.as_markup())


@router.message(Command("ai"))
async def cmd_ai(message: Message) -> None:
    from bot.callbacks import BmCb
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Открыть BotMother OS", callback_data=BmCb(action="main"))
    await message.answer(
        "🤖 <b>AI-ассистент</b>\n\n"
        "Откройте BotMother OS и перейдите в:\n"
        "<code>BotMother → 🤖 AI Assistant</code>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


@router.callback_query(AiCb.filter(F.action == "start"))
async def cb_ai_start(callback: CallbackQuery, state: FSMContext, pool: asyncpg.Pool) -> None:
    await callback.answer()
    if not await require_plan(pool, callback.from_user.id, "enterprise"):
        await callback.message.edit_text(
            "🔒 <b>AI-ассистент — ENTERPRISE</b>\n\nОформите подписку: /subscription",
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
        "читать файлы, запускать рассылки, обновлять профили и публиковать посты в каналы.\n\n"
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
    await _process_ai_turn(message, state, pool, http, message.text or "")


@router.message(AiChat.chatting, F.document)
async def msg_ai_document(
    message: Message, state: FSMContext,
    pool: asyncpg.Pool, http: aiohttp.ClientSession,
) -> None:
    try:
        file_name, file_text = await _read_attached_document(message)
    except ValueError as exc:
        await message.answer(
            "📎 <b>Не могу прочитать этот файл</b>\n\n"
            f"{escape(str(exc))}\n\n"
            f"Можно отправить: <code>{_SUPPORTED_FILE_HINT}</code>",
            parse_mode="HTML",
        )
        return
    except Exception:
        log.exception("Failed to read AI document")
        await message.answer(
            "📎 <b>Не получилось прочитать файл</b>\n\n"
            "Попробуйте отправить его еще раз или сохраните текст в формате TXT/MD.",
            parse_mode="HTML",
        )
        return

    prompt = _build_file_prompt(file_name, file_text, message.caption)
    safe_name = escape(file_name)
    await _process_ai_turn(
        message,
        state,
        pool,
        http,
        prompt,
        thinking_text=f"📎 <i>Читаю файл {safe_name}...</i>",
    )
